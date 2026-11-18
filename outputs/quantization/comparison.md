| Model | Precision | Size MiB | Latency ms (1 thr) | Latency ms (4 thr) | Peak RSS MiB (1 thr) | Test F1 | Pixel agreement | Small-change retention |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| ground_ce_final | FP32 | 4.5 | 123.65 | 54.41 | 409.3 | 0.2 | 1.0 | 1.0 |
| ground_ce_final | INT8 | 1.213 | 83.47 | 41.97 | 398.8 | 0.2 | 0.99766 | 0.85 |
| onboard_final | FP32 | 1.126 | 11.23 | 5.98 | 296.2 | 0.1923 | 1.0 | 1.0 |
| onboard_final | INT8 | 0.732 | 12.13 | 8.52 | 306.7 | 0.2 | 0.99614 | 0.7622 |
