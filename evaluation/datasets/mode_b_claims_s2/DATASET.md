# mode_b_claims_s2 — supplementary claim set (provisional, agent-authored)

34 claims (15 supported, 19 unsupported), each citing exact passages from the
frozen 632-point index (`output/mode_b_calibration/independent/passages.csv`).
The label says whether the **cited** passage states the claim; a claim that is
true of the paper but not of the passage it cites is `unsupported`
(s18, s34).

Phenomena covered: verbatim, low-overlap paraphrase, same words with changed
meaning, unsupported percentages and counts, a reversed numerical comparison,
named languages and model swaps, broad claims from one paper, "only"/"always",
unsupported and stated causes, contradiction, partial support, related-but-wrong
passage, wrong and correct paper-id references, gaps inferred from absence vs
stated gaps, prior work presented as the paper's own, future work presented as
done, dropped hedges, disagreeing evidence, elliptic negation, "none of" vs
"all ... not", reversed direction, and one claim citing two papers.

**Not independent.** The same agent wrote these claims and changed the checker
in the same session; use as a regression and development set only. Scored with
`python -m evaluation.mode_b ... --claims-supplement evaluation/datasets/mode_b_claims_s2`.
