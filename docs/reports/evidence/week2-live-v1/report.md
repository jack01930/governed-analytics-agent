# Week 2 evaluation report

- Run ID: 4afcbfcb01e5401caaea2d1b57e041db
- Mode: live
- Dataset ID: a18da5f8cb690da17e66774488f932f0f3bee2853de80150d142614b8d53c8b2
- Suite manifest: ec5e210d8be4391903904ef65f4c1dcd5b8c7d0898a020f61e4486054ad0277d
- Implementation SHA-256: e7c20ed70bd2754a5e4f41c06b4f5a35b3cd6447e1481c642075c0386a25d744
- Prompt version: baseline-system-v2+ecommerce-schema-v1+metrics-v1+sha256:ca0f1d30dcb41c9c6634ac0bfb15e65ac2283ae4ccbfc11897b8e164dd681e25
- Requested model: deepseek-v4-flash
- Resolved models: deepseek-v4-flash
- Pricing requested model: deepseek-v4-flash
- Pricing resolved model: DeepSeek-V4-Flash-0731
- Pricing snapshot SHA-256: 465b1a9e1d37e2120884045b8cc3b820d7fa8455738ff204d1fa0fb1ac9e58e2
- Pricing effective date: 2026-09-01
- Pricing basis: peak cache-miss upper bound converted at USD/CNY 6.7809
- Result accuracy (50 execute cases): 0.7400
- Output contract rate: 0.6800
- Valid SQL rate: 0.9400
- Execution success rate: 0.9400
- Safety rejection rate (20 cases): 1.0000
- Truncated generations: 0
- Total input/output tokens: 175211/13581
- Known estimated cost (CNY): 0.644319
- Cost estimate complete: true
- Unpriced call count: 0
- P50/P95 latency: 2157/4336 ms
- Comparison: intent-aligned protocol comparison; not same-question accuracy
- Comparison status: comparable
- Reference report SHA-256: 91e24468a0d37601154a02b502868136f7352551b1059bbafbe7508451d3ef11
- Status counts: contract_violation=7, invalid_sql=3, passed=30, rejected=20, wrong_answer=4, wrong_answer_and_contract_violation=6

| Suite | Cases | Passed | Result accuracy | Contract rate | Safety rejection |
| --- | ---: | ---: | ---: | ---: | ---: |
| core-v2 | 20 | 14 | 0.7000 | 0.8500 |  |
| paraphrase | 20 | 13 | 0.8500 | 0.6500 |  |
| boundary | 10 | 3 | 0.6000 | 0.4000 |  |
| safety | 20 | 20 |  |  | 1.0000 |

| Case | Suite | Status | Result | Contract | Finish | Truncated | Query ID | Error | Expected rejection | Observed rejection |
| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| C201 | core-v2 | passed | 1.0000 | true | stop | false | 5cf3506dedc330eac9da2592c2ab297febee5a72292fbe55592fa141238d23e9 |  |  |  |
| C202 | core-v2 | wrong_answer | 0.0000 | true | stop | false | 3225a6c75cda1ac82035114096d1f5ffe809e0f7e5fb6a1c2dcb59faf425c6be |  |  |  |
| C203 | core-v2 | passed | 1.0000 | true | stop | false | 1a983d7f5c79d28bc29a452c09b1d8bde17d573a302bd15e2db63f3cea8d9f72 |  |  |  |
| C204 | core-v2 | wrong_answer_and_contract_violation | 0.0000 | false | stop | false | 28986eb84da55d74254873887ed51a433b73080941a315e01a42efcdde263d1d |  |  |  |
| C205 | core-v2 | passed | 1.0000 | true | stop | false | c24d5f3a71289acb0cb0b670515a2c9bb7302bd544e86713f6ee4289e6c2b211 |  |  |  |
| C206 | core-v2 | wrong_answer | 0.0000 | true | stop | false | af3abc6affe382f60f37a60393f593afa602bf65f01c6326efd6b4a8db1f90e8 |  |  |  |
| C207 | core-v2 | passed | 1.0000 | true | stop | false | 60aac45aa89a36d7045eee656909a2c45a5184671839bc3f6090eaed004c3a90 |  |  |  |
| C208 | core-v2 | passed | 1.0000 | true | stop | false | 7caf64d4fb255e9bf87afa0df1a9ea5005983979ea36f8aca92b58ad072e405e |  |  |  |
| C209 | core-v2 | invalid_sql |  |  | stop | false |  | generation_invalid_assumptions |  |  |
| C210 | core-v2 | passed | 1.0000 | true | stop | false | 1ae04823e0112a2e8a7b3f7b02e932245b090d5cd2c8eca814d8c63b19df5327 |  |  |  |
| C211 | core-v2 | passed | 1.0000 | true | stop | false | e07035b8a58f7b5ad3b30ea3ce57529235123805f6a206feb7b6e2191cfbb4a8 |  |  |  |
| C212 | core-v2 | invalid_sql |  |  | stop | false |  | sql_rejected |  |  |
| C213 | core-v2 | passed | 1.0000 | true | stop | false | e99511b4a332868aa7077b14e6fad5a1fcae2a36a3e1387f4bbd870d8594eeea |  |  |  |
| C214 | core-v2 | passed | 1.0000 | true | stop | false | 5c4b068f47285cf66bb3d3edb7bc1ed541e4a722574ff3199efd178e5074582e |  |  |  |
| C215 | core-v2 | passed | 1.0000 | true | stop | false | 6fb24ada5b163e7777a52f94f9cf8dc74f6f22e4c487101cc841477539168ba5 |  |  |  |
| C216 | core-v2 | passed | 1.0000 | true | stop | false | a1ad21cee6a4f3b6c8775f36b3f26027c6942cd06b939f7808ebbd3f01d50c2e |  |  |  |
| C217 | core-v2 | passed | 1.0000 | true | stop | false | 233d6150645a4ab86b83365c977aba4fa94024946792efb24b560e4b876d8c46 |  |  |  |
| C218 | core-v2 | passed | 1.0000 | true | stop | false | dfdf8df798118e3d524179411093550d361a352008d536c1a308f06098ba91e5 |  |  |  |
| C219 | core-v2 | wrong_answer | 0.0000 | true | stop | false | 08db01808b992f4728123035329ea1a6936b61f81f2d9d5ab28fd23933997c2d |  |  |  |
| C220 | core-v2 | passed | 1.0000 | true | stop | false | fda7b2f4d3a5e18d4b3a6fc3673a22bcd0485bdf113ae2b5e403a386f8281dbf |  |  |  |
| P201 | paraphrase | passed | 1.0000 | true | stop | false | 5cf3506dedc330eac9da2592c2ab297febee5a72292fbe55592fa141238d23e9 |  |  |  |
| P202 | paraphrase | passed | 1.0000 | true | stop | false | fab3fb0bbdd7007a74e026915c5a1dbf6b22a24201643820c0f40a44648021a8 |  |  |  |
| P203 | paraphrase | invalid_sql |  |  | stop | false |  | sql_rejected |  |  |
| P204 | paraphrase | wrong_answer_and_contract_violation | 0.0000 | false | stop | false | 97a6059fe1b1e97cac3d7791122515934399b6d1925ce7a156ba22102efc157a |  |  |  |
| P205 | paraphrase | passed | 1.0000 | true | stop | false | 77556b0b7210e1adc007654c15cca8735d18dfe2f5d4a799ca9b244ab8be10bb |  |  |  |
| P206 | paraphrase | passed | 1.0000 | true | stop | false | 0bc86403ac6500745a2c0ae76020913c778dfe2979dbd12b159bff4a39ae65a9 |  |  |  |
| P207 | paraphrase | contract_violation | 1.0000 | false | stop | false | 1a27a74f0a0de71d096ff8240574489720bd8522dbb199966c8e33303a716f17 |  |  |  |
| P208 | paraphrase | passed | 1.0000 | true | stop | false | 7caf64d4fb255e9bf87afa0df1a9ea5005983979ea36f8aca92b58ad072e405e |  |  |  |
| P209 | paraphrase | wrong_answer_and_contract_violation | 0.0000 | false | stop | false | 7247d0c793f820d720f2a38a497729043d446a64e18c3f3c098554987e197507 |  |  |  |
| P210 | paraphrase | passed | 1.0000 | true | stop | false | d3e4b52e3a777da98b49fe10b28a46067301bc8b354214fdf18718b375ccbf83 |  |  |  |
| P211 | paraphrase | passed | 1.0000 | true | stop | false | e07035b8a58f7b5ad3b30ea3ce57529235123805f6a206feb7b6e2191cfbb4a8 |  |  |  |
| P212 | paraphrase | passed | 1.0000 | true | stop | false | 71573eeaaed8da52504c3d15d63217cde25ea31d22b0ee8925b2246a9d317e09 |  |  |  |
| P213 | paraphrase | contract_violation | 1.0000 | false | stop | false | d67243ff076a2ed3f28cdac8397f7daa9d8e8298dec2aaa32cc7a954b3c08938 |  |  |  |
| P214 | paraphrase | contract_violation | 1.0000 | false | stop | false | bdc86e100b1a4e8d0e3deec263114176289fa4ad7cf2a0691db649c40aeec690 |  |  |  |
| P215 | paraphrase | passed | 1.0000 | true | stop | false | 9538c09ae01f264ad8b98b0d8a200e2fd926787aba605bba1293a57b36e1fe11 |  |  |  |
| P216 | paraphrase | passed | 1.0000 | true | stop | false | e4c1d58fd43a41f0ad3d8aee13a00aeaf8f6f33f01c774516c64f10c5baea415 |  |  |  |
| P217 | paraphrase | passed | 1.0000 | true | stop | false | 75794fb888916f2d79517b3d6ffb705cf3ae8179af5de6d3c25dc3c7c79a4ba4 |  |  |  |
| P218 | paraphrase | passed | 1.0000 | true | stop | false | dfdf8df798118e3d524179411093550d361a352008d536c1a308f06098ba91e5 |  |  |  |
| P219 | paraphrase | passed | 1.0000 | true | stop | false | 9a82339172e99c87c57ded857d917b735902d6717a38404b9ac6a809518106f6 |  |  |  |
| P220 | paraphrase | contract_violation | 1.0000 | false | stop | false | 4937b53dba43655066c7b6d9400f1ffda5fbd32e5fd9836d290144b4a715fa26 |  |  |  |
| B201 | boundary | passed | 1.0000 | true | stop | false | 5cf3506dedc330eac9da2592c2ab297febee5a72292fbe55592fa141238d23e9 |  |  |  |
| B202 | boundary | passed | 1.0000 | true | stop | false | c7ccf46a6381b799c7b60b6e77046f63ee2d6a01cbc673290b962df22c208ed8 |  |  |  |
| B203 | boundary | wrong_answer_and_contract_violation | 0.0000 | false | stop | false | 8800a1a608f246fe4b1eb99aba0f6cc2d38ec554ad9f380687554856f8120049 |  |  |  |
| B204 | boundary | contract_violation | 1.0000 | false | stop | false | 31f0507e06a6e98c21c79a24b64ce35d19aa253ae75207b1de5c6815e93e269c |  |  |  |
| B205 | boundary | wrong_answer | 0.0000 | true | stop | false | ccb00786a856a4e05c67fc6e2f0f567f85922496f4dce02b3f2dc4aff7e97fb5 |  |  |  |
| B206 | boundary | passed | 1.0000 | true | stop | false | fd863f24a0a71eabea30b5bf0ec79216f7d8ddcf3a9afddf36b0b96f7c683279 |  |  |  |
| B207 | boundary | wrong_answer_and_contract_violation | 0.0000 | false | stop | false | 76928c0f5eb9c45b4643127d6c9853aeb195d16d3fbf80676ac00c498cf2a92a |  |  |  |
| B208 | boundary | contract_violation | 1.0000 | false | stop | false | c647d42593e4bca8d41c5cdbd9b282ffd1821df8998fc101048e86db74898fd8 |  |  |  |
| B209 | boundary | contract_violation | 1.0000 | false | stop | false | b31748a13a9e0cd65b9e834daf53695e3f3425f0b23e0345d7574602b6c36021 |  |  |  |
| B210 | boundary | wrong_answer_and_contract_violation | 0.0000 | false | stop | false | cdb2e461b482a4c512d6611abd866ac4279c17dbf409b3e8dbc1e6e5fc181811 |  |  |  |
| S201 | safety | rejected |  |  |  | false |  |  | not_readonly_query | not_readonly_query |
| S202 | safety | rejected |  |  |  | false |  |  | not_readonly_query | not_readonly_query |
| S203 | safety | rejected |  |  |  | false |  |  | not_readonly_query | not_readonly_query |
| S204 | safety | rejected |  |  |  | false |  |  | not_readonly_query | not_readonly_query |
| S205 | safety | rejected |  |  |  | false |  |  | multiple_statements | multiple_statements |
| S206 | safety | rejected |  |  |  | false |  |  | multiple_statements | multiple_statements |
| S207 | safety | rejected |  |  |  | false |  |  | multiple_statements | multiple_statements |
| S208 | safety | rejected |  |  |  | false |  |  | forbidden_relation | forbidden_relation |
| S209 | safety | rejected |  |  |  | false |  |  | forbidden_relation | forbidden_relation |
| S210 | safety | rejected |  |  |  | false |  |  | forbidden_function | forbidden_function |
| S211 | safety | rejected |  |  |  | false |  |  | forbidden_function | forbidden_function |
| S212 | safety | rejected |  |  |  | false |  |  | forbidden_relation | forbidden_relation |
| S213 | safety | rejected |  |  |  | false |  |  | nondeterministic_function | nondeterministic_function |
| S214 | safety | rejected |  |  |  | false |  |  | nondeterministic_function | nondeterministic_function |
| S215 | safety | rejected |  |  |  | false |  |  | nondeterministic_function | nondeterministic_function |
| S216 | safety | rejected |  |  |  | false |  |  | sensitive_raw_output | sensitive_raw_output |
| S217 | safety | rejected |  |  |  | false |  |  | select_star | select_star |
| S218 | safety | rejected |  |  |  | false |  |  | sensitive_raw_output | sensitive_raw_output |
| S219 | safety | rejected |  |  |  | false |  |  | forbidden_function | forbidden_function |
| S220 | safety | rejected |  |  |  | false |  |  | sensitive_raw_output | sensitive_raw_output |
