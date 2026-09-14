"""Dual sentiment scoring: FinBERT (finance-tuned) + VADER (general lexicon).

Both are run on every piece of text and logged separately rather than fused into
one number. FinBERT (yiyanghkust/finbert-tone) is trained on financial-phrasebank
and analyst-report language -- it's tuned for earnings/guidance tone, not
militarized geopolitical phrasing. VADER is a general-purpose lexicon: cruder,
but it at least reacts to plain-English intensity words ("strikes", "seizes")
that FinBERT may not register as financial sentiment at all. Keeping both lets
you see where they disagree instead of trusting either one blindly on text it
wasn't built for.
"""
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_FINBERT_MODEL_NAME = "yiyanghkust/finbert-tone"
_FINBERT_LABELS = ["Positive", "Negative", "Neutral"]

_finbert_model = None
_finbert_tokenizer = None
_vader = SentimentIntensityAnalyzer()


def _load_finbert():
    global _finbert_model, _finbert_tokenizer
    if _finbert_model is None:
        _finbert_tokenizer = AutoTokenizer.from_pretrained(_FINBERT_MODEL_NAME)
        _finbert_model = AutoModelForSequenceClassification.from_pretrained(_FINBERT_MODEL_NAME)
        _finbert_model.eval()
    return _finbert_model, _finbert_tokenizer


def finbert_polarity(text: str) -> float:
    """Signed polarity in [-1, 1]: P(Positive) - P(Negative). Neutral mass is dropped."""
    if not text or not text.strip():
        return 0.0
    model, tokenizer = _load_finbert()
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    with torch.no_grad():
        logits = model(**inputs).logits
    probs = torch.softmax(logits, dim=1).numpy()[0]
    pos = probs[_FINBERT_LABELS.index("Positive")]
    neg = probs[_FINBERT_LABELS.index("Negative")]
    return float(pos - neg)


def vader_polarity(text: str) -> float:
    """VADER compound score, already in [-1, 1]."""
    if not text or not text.strip():
        return 0.0
    return _vader.polarity_scores(text)["compound"]


def score_text(text: str) -> dict:
    """Run both models on the same text. Kept as two independent numbers on purpose."""
    return {
        "finbert": finbert_polarity(text),
        "vader": vader_polarity(text),
    }
