# Five Nemotron models, same frauds

The question: does HumanKey need a big model to be safe? We ran the same two held-out companies (seeds 108
and 109: 34 invoices, 18 planted frauds) with five NVIDIA Nemotron models, from 4 billion to 550 billion
parameters. Same code, same prompts, same Zetaris data, same rules (auto-pay under $4,000, phone up to
$10,000, hardware key above that or on any risk).

| Model | Size | Where it ran | Agent alone: frauds paid | HumanKey: frauds paid with no human | Clean paid automatically | Clean to the phone | Sent to the key | Mean time per call | Model cost, 2 companies |
|---|---|---|---|---|---|---|---|---|---|
| `nemotron-mini` | 4.2B | laptop GPU (Quadro P3000) | 10 | **0** | 1 | 5 | 22 | 5 s | $0 |
| `nemotron-3-nano-30b-a3b` | 30B, 3B active | hosted | 4 | **0** | 1 | 5 | 22 | 8 s | $0.02 |
| `nemotron-3.5-lightning` | | hosted | 11 | **0** | 1 | 5 | 22 | 19 s | $0.04 |
| `nemotron-3-super-120b-a12b` | 120B, 12B active | hosted | 6 | **0** | 2 | 4 | 22 | 19 s | $0.04 |
| `nemotron-3-ultra-550b-a55b` | 550B, 55B active | hosted | 9 | **0** | 2 | 4 | 22 | 7 s | $0.32 |

"Agent alone" is configuration B of the benchmark: the model's first answer decides, with no critic and no
key. "Sent to the key" counts 12 frauds and 10 clean invoices over $10,000; 4 more frauds were blocked
outright and 2 were caught by the count witness, for every model.

What we read from it:
- **Safety does not come from model size.** With the agent alone, every Nemotron paid some frauds (4 to 11
  of 18), and the biggest model was not the safest. Inside HumanKey every one of them paid none, because the
  risk rule, the critic and the hardware key do not depend on what the model says.
- **Model size changes the human's workload, a little.** Super and Ultra let one more clean invoice pay
  itself than the smaller models; everything else ended in the same lane.
- **So pick the smallest model that keeps the workload acceptable.** Here that is the 4B model on the
  laptop: free, private (invoice data never leaves the machine) and as safe as the 550B one.

Limits: two companies are a small sample, the time per call depends on the gateway and the network, and
model cost is the gateway's list price for the tokens each run used (Zetaris time is not included).
Hosted models were called through an OpenAI-compatible gateway routing to OpenRouter, with the same
provider code as Nightshift. Reports: `BENCHMARK_nano.md`, `BENCHMARK_lightning.md`, `BENCHMARK_super.md`,
`BENCHMARK_ultra.md`; the 4B run is the held-out section of `BENCHMARK.md`. Every model call is logged in
`evidence/llm/<model>/`.
