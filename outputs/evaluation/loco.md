| Direction | Windows | Method | k | P | R | F1 (95% CI) | Natural false-alarm rate | AP | AUROC (95% CI) |
|---|---|---|---:|---:|---:|---|---:|---:|---|
| mumbai_to_delhi | all target (n=167, pos=20) | loco_model | 8 | 0.121 | 0.95 | 0.2147 [0.1387, 0.2903] | 0.9441 | 0.085 | 0.3014 [0.2082, 0.4022] |
| mumbai_to_delhi | all target (n=167, pos=20) | rule_baseline | 128 | 0.0974 | 0.75 | 0.1724 [0.0982, 0.2444] | 0.9441 | 0.0752 | 0.2056 [0.1138, 0.3187] |
| mumbai_to_delhi | all target (n=167, pos=20) | loco_window_filter | 0.5698 | 0.1515 | 0.75 | 0.2521 [0.1468, 0.3529] | 0.5734 | 0.2023 | 0.6017 [0.4294, 0.7587] |
| mumbai_to_delhi | target held-out (n=83, pos=9) | loco_model | 8 | 0.1184 | 1.0 | 0.2118 [0.1, 0.3226] | 0.9167 | 0.0962 | 0.4062 [0.2467, 0.5737] |
| mumbai_to_delhi | target held-out (n=83, pos=9) | rule_baseline | 128 | 0.0897 | 0.7778 | 0.1609 [0.0506, 0.2737] | 0.9583 | 0.0849 | 0.3243 [0.1563, 0.5076] |
| mumbai_to_delhi | target held-out (n=83, pos=9) | in_city_ground_ce_final | 128 | 0.1077 | 0.7778 | 0.1892 [0.0625, 0.3171] | 0.7917 | 0.0788 | 0.268 [0.1487, 0.3917] |
| delhi_to_mumbai | all target (n=77, pos=4) | loco_model | 1 | 0.087 | 1.0 | 0.16 [0.0392, 0.3052] | 0.5634 | 0.0643 | 0.5274 [0.3831, 0.68] |
| delhi_to_mumbai | all target (n=77, pos=4) | rule_baseline | 16 | 0.087 | 1.0 | 0.16 [0.0392, 0.3052] | 0.5634 | 0.0666 | 0.5445 [0.4, 0.6973] |
| delhi_to_mumbai | all target (n=77, pos=4) | loco_window_filter | 0.6054 | 0.0541 | 0.5 | 0.0976 [0.0, 0.2287] | 0.4789 | 0.0569 | 0.4589 [0.3122, 0.6146] |
| delhi_to_mumbai | target held-out (n=39, pos=1) | loco_model | 1 | 0.0417 | 1.0 | 0.08 [0.0, 0.2581] | 0.5946 | 0.0667 | 0.6316 [0.4737, 0.7895] |
| delhi_to_mumbai | target held-out (n=39, pos=1) | rule_baseline | 16 | 0.0417 | 1.0 | 0.08 [0.0, 0.2581] | 0.5946 | 0.0435 | 0.4211 [0.2671, 0.5884] |
| delhi_to_mumbai | target held-out (n=39, pos=1) | in_city_ground_ce_final | 128 | 0.0417 | 1.0 | 0.08 [0.0, 0.2581] | 0.5946 | 0.0667 | 0.6316 [0.4737, 0.7942] |
