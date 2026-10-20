// A real /query response: live run LV-1b (2026-10-18), evaluation/output/mode_b_eval/
// run-20261018-v1/live/LV-1b.json. Citation text restored from the stored passage
// (live_check verified it equals the stored text cut at 1,200 characters).
export const liveGroundedAnswer = {
  "outcome": "grounded_answer",
  "answer": "Turkish had the highest full compliance rate.",
  "claims": [
    {
      "text": "Turkish had the highest full compliance rate.",
      "evidence_ids": [
        "b77a41e9-5293-53e5-8681-48fc220b28c9"
      ]
    }
  ],
  "citations": [
    {
      "id": "b77a41e9-5293-53e5-8681-48fc220b28c9",
      "paper_id": 3,
      "title": "An Empirical Evaluation of Prompt Injection Vulnerabilities in Large Language Models Across Multilingual and Obfuscated Attack Scenarios",
      "year": 2026,
      "source_type": "paragraph",
      "text": "The language or encoding used for the malicious request has a marked effect on compliance.Table III reports full compliance, stalling, refusal, fulfillment, and warning rates for the four natural languages and three encodings.Turkish shows the highest full compliance (80.18%), followed by Simplified Chinese (77.52%),Russian (73.92%), and English (71.31%).In contrast, Hex (54.41%),ROT13 (58.38%), and Base64 (65.59%) have substantially lower full compliance rates, though they still produce malicious outputs in a majority of cases."
    }
  ],
  "grounded": true,
  "retrieval_rounds": 1,
  "retry_count": 0,
  "regeneration_count": 0,
  "trace": [
    "retrieve:round=1;vector=12;graph=5;total=17",
    "assess:sufficient=True;relevant=12;papers=4",
    "rerank",
    "generate:strict=False",
    "grounding:structural=True;grounded=True;claims=1;cited=1;invalid_citations=0;unsupported=0;reason=ok;vetoed=0;prose_uncovered=1;veto_codes=-"
  ],
  "provider": "groq",
  "model": "openai/gpt-oss-20b"
} as const
