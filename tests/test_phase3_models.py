import pytest
import torch

from src.models.siamese import (
    GROUND_CLASSES,
    GROUND_BANDS,
    ONBOARD_BANDS,
    SiameseMobileNetV3Small,
    SiameseUNet,
    count_parameters,
)


@pytest.mark.parametrize(
    ("model", "channels", "classes"),
    [
        (SiameseUNet(), len(GROUND_BANDS), len(GROUND_CLASSES)),
        (SiameseMobileNetV3Small(), len(ONBOARD_BANDS), 2),
    ],
)
def test_siamese_models_return_full_resolution_logits(model, channels, classes):
    before = torch.rand(1, channels, 64, 64)
    after = torch.rand_like(before)

    logits = model(before, after)

    assert logits.shape == (1, classes, 64, 64)
    assert torch.isfinite(logits).all()


def test_ground_model_produces_gradients_and_seven_class_schema():
    model = SiameseUNet(widths=(8, 16, 32))
    before = torch.rand(2, len(GROUND_BANDS), 32, 32)
    after = torch.rand_like(before)

    model(before, after).mean().backward()

    assert model.classifier.weight.grad is not None
    assert len(GROUND_CLASSES) == 7


def test_onboard_model_is_under_five_million_parameters():
    model = SiameseMobileNetV3Small()

    assert count_parameters(model) < 5_000_000


@pytest.mark.parametrize(
    "model",
    [SiameseUNet(), SiameseMobileNetV3Small()],
)
def test_model_rejects_mismatched_or_wrong_channel_inputs(model):
    expected = model.in_channels
    image = torch.rand(1, expected, 32, 32)

    with pytest.raises(ValueError, match="shapes differ"):
        model(image, torch.rand(1, expected, 32, 31))
    with pytest.raises(ValueError, match="input channels"):
        model(torch.rand(1, expected + 1, 32, 32), image)
