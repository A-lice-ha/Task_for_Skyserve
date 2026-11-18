| Model | Precision | Size MiB | Latency ms (1 thr) | Latency ms (4 thr) | Peak RSS MiB (1 thr) | Test F1 | Pixel agreement | Small-change retention |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| ground_ce_final | FP32 | 4.5 | 151.32 | 82.92 | 397.5 | 0.2 | 1.0 | 1.0 |
| ground_ce_final | INT8 | 1.213 | 120.79 | 58.42 | 399.2 | 0.1923 | 0.99859 | 0.8963 |
| onboard_final | FP32 | 1.126 | 18.35 | 8.32 | 296.3 | 0.1923 | 1.0 | 1.0 |
| onboard_final | INT8 | 0.732 | 20.66 | 12.54 | 306.4 | 0.1923 | 0.99603 | 0.8742 |
