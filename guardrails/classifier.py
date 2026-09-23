"""L2: a trained classifier behind the InjectionClassifier protocol.

    meta-llama/Llama-Prompt-Guard-2-86M -- mDeBERTa-base, binary benign/malicious,
    512-token window, reported AUC 0.998 English / 0.995 multilingual and recall
    97.5% at 1% FPR on Meta's held-out benchmark.

THE REASON THIS IS A SEPARATE, OPT-IN MODULE and not wired on by default is written on
Meta's own model card. The languages it was evaluated in are English, French, German,
Hindi, Italian, Portuguese, Spanish and Thai. Vietnamese is not among them. mDeBERTa
covers Vietnamese, so the model will return confident scores on Vietnamese input and
nobody has published what those scores are worth. Switching it on by reputation would put
a number in the report that was never measured on the traffic this system serves.

So: the adapter exists, the threshold is a parameter, and bench/scripts/eval_defenses.py
measures it on this project's own adversarial suite before anyone claims a figure.

TWO OPERATIONAL CONSTRAINTS that shape where it can run.

  512 tokens. The model card: "For longer inputs, split prompts into segments and scan
  them in parallel." A retrieved chunk here is ~918 characters, which is already near the
  limit in Vietnamese at the measured 30.2 tokens per 100 characters. `score` therefore
  windows long input and takes the maximum, which is the correct aggregation for a
  detector -- one malicious window makes the whole document malicious.

  It is a 86M-parameter transformer. On CPU that is tens of milliseconds per window and
  it lands on the TTFT budget; on the GPU it competes with vLLM for the same card. The
  honest place for it is the second time-slicing slot, and the honest thing to record is
  its latency alongside its accuracy.

LICENCE. Llama 4 Community License, gated on Hugging Face, and it carries an attribution
obligation ("Built with Llama") on anything that uses it. That is a real constraint for a
deliverable, and it is the reason `PromptGuardClassifier` raises a clear error rather than
silently falling back when the weights are absent.
"""

from __future__ import annotations

from dataclasses import dataclass

MODEL_ID = "meta-llama/Llama-Prompt-Guard-2-86M"
# Model card: both Prompt Guard 2 models support a 512-token context window.
MAX_TOKENS = 512
# Overlap so an instruction straddling a window boundary is not cut in half.
WINDOW_STRIDE = 384


@dataclass
class PromptGuardClassifier:
    """Implements guardrails.injection.InjectionClassifier.

    Returns P(malicious) in [0, 1]. The caller applies the threshold, because the right
    operating point depends on what the false positives cost, and here they cost a
    blocked question from a real analyst.
    """

    model_id: str = MODEL_ID
    device: str | None = None
    _tok = None
    _model = None

    def __post_init__(self) -> None:
        try:
            import torch  # noqa: F401
            from transformers import (AutoModelForSequenceClassification,
                                      AutoTokenizer)
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "Prompt Guard needs torch + transformers. This is deliberately not a "
                "dependency of the serving path: run it in the embedding venv, or in "
                "the cluster beside vLLM."
            ) from e

        try:
            self._tok = AutoTokenizer.from_pretrained(self.model_id)
            self._model = AutoModelForSequenceClassification.from_pretrained(
                self.model_id)
        except Exception as e:  # pragma: no cover - network/licence dependent
            raise RuntimeError(
                f"could not load {self.model_id}. It is gated behind the Llama 4 "
                "Community License: accept the terms on Hugging Face and set HF_TOKEN. "
                "Anything shipped using it must carry 'Built with Llama' attribution."
            ) from e
        self._model.eval()

    def _windows(self, text: str) -> list[str]:
        ids = self._tok.encode(text, add_special_tokens=False)
        if len(ids) <= MAX_TOKENS - 2:
            return [text]
        out = []
        for start in range(0, len(ids), WINDOW_STRIDE):
            piece = ids[start:start + MAX_TOKENS - 2]
            if not piece:
                break
            out.append(self._tok.decode(piece))
            if start + MAX_TOKENS - 2 >= len(ids):
                break
        return out

    def score(self, text: str) -> float:
        import torch

        best = 0.0
        for window in self._windows(text):
            enc = self._tok(window, return_tensors="pt", truncation=True,
                            max_length=MAX_TOKENS)
            with torch.no_grad():
                logits = self._model(**enc).logits
            probs = torch.softmax(logits, dim=-1)[0]
            # Label order comes from the model config rather than an assumed index:
            # getting this backwards yields a detector that passes every attack and
            # blocks every question, and the numbers look plausible either way.
            label_id = next(
                (i for i, name in self._model.config.id2label.items()
                 if str(name).upper().startswith("MALICIOUS")),
                probs.argmax().item(),
            )
            best = max(best, float(probs[label_id]))
        return best
