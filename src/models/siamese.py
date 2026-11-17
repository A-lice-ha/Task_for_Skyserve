"""Shared-weight Siamese models for bi-temporal Sentinel-2 change detection."""

from __future__ import annotations

from collections.abc import Sequence

import torch
from torch import nn
from torch.nn import functional as F
from torchvision.models import mobilenet_v3_small


GROUND_CLASSES = (
    "no_change",
    "construction",
    "demolition",
    "road",
    "bridge",
    "infrastructure_other",
    "uncertain",
)
GROUND_BANDS = ("B02", "B03", "B04", "B08", "B11", "B12")
ONBOARD_BANDS = ("B02", "B03", "B04", "B08")


def _validate_pair(
    before: torch.Tensor,
    after: torch.Tensor,
    expected_channels: int,
) -> None:
    if before.ndim != 4 or after.ndim != 4:
        raise ValueError("Inputs must have shape (batch, channels, height, width)")
    if before.shape[1] != expected_channels or after.shape[1] != expected_channels:
        raise ValueError(
            f"Expected {expected_channels} input channels for each date, received "
            f"{before.shape[1]} and {after.shape[1]}"
        )
    if before.shape != after.shape:
        raise ValueError(
            f"Before/after input shapes differ: {before.shape} != {after.shape}"
        )


class ConvBlock(nn.Sequential):
    def __init__(self, in_channels: int, out_channels: int, stride: int = 1):
        super().__init__(
            nn.Conv2d(
                in_channels, out_channels, 3, stride=stride, padding=1, bias=False
            ),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, 3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )


class SharedEncoder(nn.Module):
    def __init__(self, in_channels: int, widths: Sequence[int]):
        super().__init__()
        blocks = []
        current_channels = in_channels
        for index, width in enumerate(widths):
            blocks.append(
                ConvBlock(
                    current_channels,
                    width,
                    stride=1 if index == 0 else 2,
                )
            )
            current_channels = width
        self.blocks = nn.ModuleList(blocks)

    def forward(self, image: torch.Tensor) -> list[torch.Tensor]:
        features = []
        for block in self.blocks:
            image = block(image)
            features.append(image)
        return features


class SiameseUNet(nn.Module):
    """Six-band, seven-class Siamese U-Net; returns per-class logits."""

    def __init__(
        self,
        in_channels: int = len(GROUND_BANDS),
        num_classes: int = len(GROUND_CLASSES),
        widths: Sequence[int] = (32, 64, 128, 256),
    ):
        super().__init__()
        if in_channels <= 0 or num_classes <= 0 or len(widths) < 2:
            raise ValueError("Channels/classes must be positive and need two stages")
        if any(width <= 0 for width in widths):
            raise ValueError("All encoder widths must be positive")
        self.in_channels = in_channels
        self.num_classes = num_classes
        self.encoder = SharedEncoder(in_channels, widths)
        self.bottom = ConvBlock(widths[-1] * 3, widths[-1])
        self.decoder = nn.ModuleList(
            ConvBlock(widths[index + 1] + widths[index] * 3, widths[index])
            for index in range(len(widths) - 2, -1, -1)
        )
        self.classifier = nn.Conv2d(widths[0], num_classes, kernel_size=1)

    @staticmethod
    def _fuse(before: torch.Tensor, after: torch.Tensor) -> torch.Tensor:
        return torch.cat((before, after, torch.abs(after - before)), dim=1)

    def forward(self, before: torch.Tensor, after: torch.Tensor) -> torch.Tensor:
        _validate_pair(before, after, self.in_channels)
        before_features = self.encoder(before)
        after_features = self.encoder(after)
        x = self.bottom(
            self._fuse(before_features[-1], after_features[-1])
        )
        for decoder, index in zip(
            self.decoder, range(len(before_features) - 2, -1, -1)
        ):
            skip = self._fuse(before_features[index], after_features[index])
            x = F.interpolate(x, size=skip.shape[-2:], mode="bilinear", align_corners=False)
            x = decoder(torch.cat((x, skip), dim=1))
        return self.classifier(x)


class DepthwiseSeparableBlock(nn.Sequential):
    def __init__(self, in_channels: int, out_channels: int):
        super().__init__(
            nn.Conv2d(
                in_channels,
                in_channels,
                3,
                padding=1,
                groups=in_channels,
                bias=False,
            ),
            nn.BatchNorm2d(in_channels),
            nn.Hardswish(inplace=True),
            nn.Conv2d(in_channels, out_channels, 1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.Hardswish(inplace=True),
        )


class SiameseMobileNetV3Small(nn.Module):
    """Four-band half-width MobileNetV3-Small with a binary output head."""

    feature_indices = (1, 3, 8, 12)

    def __init__(self, in_channels: int = len(ONBOARD_BANDS), width_mult: float = 0.5):
        super().__init__()
        if in_channels <= 0 or not 0 < width_mult <= 1:
            raise ValueError("Input channels must be positive and width_mult in (0, 1]")
        self.in_channels = in_channels
        self.encoder = mobilenet_v3_small(
            weights=None, width_mult=width_mult
        ).features
        first_conv = self.encoder[0][0]
        self.encoder[0][0] = nn.Conv2d(
            in_channels,
            first_conv.out_channels,
            kernel_size=first_conv.kernel_size,
            stride=first_conv.stride,
            padding=first_conv.padding,
            bias=False,
        )
        feature_channels = tuple(
            self._output_channels(self.encoder[index])
            for index in self.feature_indices
        )
        decoder_widths = (48, 32, 24)
        self.bottom = DepthwiseSeparableBlock(feature_channels[-1], decoder_widths[0])
        self.decoder = nn.ModuleList(
            DepthwiseSeparableBlock(
                decoder_widths[index] + feature_channels[-2 - index],
                decoder_widths[index + 1],
            )
            for index in range(len(decoder_widths) - 1)
        )
        self.final_refine = DepthwiseSeparableBlock(
            decoder_widths[-1] + feature_channels[0], 16
        )
        self.classifier = nn.Conv2d(16, 2, kernel_size=1)

    @staticmethod
    def _output_channels(layer: nn.Module) -> int:
        if hasattr(layer, "out_channels"):
            return int(layer.out_channels)
        raise TypeError(f"Cannot determine output channels for {type(layer).__name__}")

    def _encode(self, image: torch.Tensor) -> list[torch.Tensor]:
        outputs = []
        for index, layer in enumerate(self.encoder):
            image = layer(image)
            if index in self.feature_indices:
                outputs.append(image)
        return outputs

    def forward(self, before: torch.Tensor, after: torch.Tensor) -> torch.Tensor:
        _validate_pair(before, after, self.in_channels)
        before_features = self._encode(before)
        after_features = self._encode(after)
        differences = [
            torch.abs(after_feature - before_feature)
            for before_feature, after_feature in zip(before_features, after_features)
        ]
        x = self.bottom(differences[-1])
        for decoder, skip in zip(self.decoder, reversed(differences[1:-1])):
            x = F.interpolate(x, size=skip.shape[-2:], mode="bilinear", align_corners=False)
            x = decoder(torch.cat((x, skip), dim=1))
        x = F.interpolate(
            x, size=differences[0].shape[-2:], mode="bilinear", align_corners=False
        )
        x = self.final_refine(torch.cat((x, differences[0]), dim=1))
        x = F.interpolate(x, size=before.shape[-2:], mode="bilinear", align_corners=False)
        return self.classifier(x)


def count_parameters(model: nn.Module) -> int:
    """Return the number of trainable parameters."""
    return sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)
