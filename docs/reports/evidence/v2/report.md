# Baseline evaluation report

- Pricing effective date: 2026-09-01
- Pricing basis: peak cache-miss upper bound converted at USD/CNY 6.7809
- Run ID: 65c87bf0355047a9b23243f70756e3f4
- Mode: live
- Dataset ID: a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2
- Prompt version: baseline-system-v2+ecommerce-schema-v1+metrics-v1+sha256:ca0f1d30dcb41c9c6634ac0bfb15e65ac2283ae4ccbfc11897b8e164dd681e25
- Requested model: deepseek-v4-flash
- Resolved models: deepseek-v4-flash
- Result accuracy: 0.2500
- Valid SQL rate: 0.7000
- Execution success rate: 0.7000
- Total cost (CNY): 0.259588
- Average cost (CNY): 0.012979
- P50 latency: 2565 ms
- P95 latency: 5402 ms
- Status counts: passed=5, wrong_answer=9, invalid_sql=6, execution_error=0

| Case | Status | Score | Latency (ms) | Input tokens | Output tokens | Cost (CNY) | Error type |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| G001 | passed | 1.0000 | 5402 | 3435 | 189 | 0.011940 |  |
| G002 | invalid_sql | 0.0000 | 2565 | 3424 | 319 | 0.013071 | sql_rejected |
| G003 | wrong_answer | 0.0000 | 3455 | 3424 | 436 | 0.014118 |  |
| G004 | invalid_sql | 0.0000 | 3869 | 3424 | 498 | 0.014673 | sql_rejected |
| G005 | invalid_sql | 0.0000 | 4435 | 3426 | 599 | 0.015583 | sql_rejected |
| G006 | invalid_sql | 0.0000 | 6161 | 3422 | 1200 | 0.020951 | generation_invalid_json |
| G007 | passed | 1.0000 | 1818 | 3427 | 134 | 0.011424 |  |
| G008 | wrong_answer | 0.0000 | 2546 | 3425 | 306 | 0.012958 |  |
| G009 | wrong_answer | 0.0000 | 3451 | 3428 | 401 | 0.013817 |  |
| G010 | wrong_answer | 0.0000 | 2090 | 3422 | 165 | 0.011687 |  |
| G011 | passed | 1.0000 | 2678 | 3428 | 141 | 0.011490 |  |
| G012 | wrong_answer | 0.0000 | 2345 | 3428 | 173 | 0.011776 |  |
| G013 | passed | 1.0000 | 2148 | 3426 | 151 | 0.011573 |  |
| G014 | wrong_answer | 0.0000 | 1646 | 3426 | 137 | 0.011448 |  |
| G015 | passed | 1.0000 | 2649 | 3428 | 279 | 0.012725 |  |
| G016 | wrong_answer | 0.0000 | 3145 | 3426 | 303 | 0.012934 |  |
| G017 | invalid_sql | 0.0000 | 1884 | 3426 | 135 | 0.011430 | sql_rejected |
| G018 | wrong_answer | 0.0000 | 2545 | 3426 | 145 | 0.011520 |  |
| G019 | wrong_answer | 0.0000 | 1878 | 3427 | 159 | 0.011648 |  |
| G020 | invalid_sql | 0.0000 | 4642 | 3427 | 290 | 0.012821 | sql_rejected |
