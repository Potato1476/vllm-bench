"""Vietnamese text normalisation and lexical tokenisation.

WHY NOT A WORD SEGMENTER. The obvious move for Vietnamese retrieval is underthesea or
pyvi, which turn "chuyến hoàn thành" into the single token "chuyến_hoàn_thành". They are
better at that job than anything here. They also pull in a few hundred MB of models and
become a second thing to version, deploy and keep alive inside the request path, for a
corpus of 798 chunks.

The cheaper approximation: index syllable unigrams AND adjacent bigrams. Vietnamese words
are overwhelmingly one or two syllables, so the bigram stream recovers most compound terms
("hoàn thành", "tài xế", "chuyến xe") as single index units without a segmenter. A trigram
term like "tỷ lệ huỷ chuyến" still matches through its constituent bigrams, with the usual
BM25 saturation keeping the score sane.

The cost is index size, roughly double, which for this corpus is nothing.

NORMALISATION IS SHARED WITH THE PROMPT LAYER. The same normalise() runs on text before
indexing, before querying, and before a chunk is pasted into a prompt. That last one is
what makes prefix caching work: vLLM hashes token ids, so two requests only share a cache
prefix if their text is byte-identical. Vietnamese arrives from the web in both NFC and
NFD -- "ế" is one codepoint or two -- and the two forms tokenise differently while looking
identical on screen. Normalising once, everywhere, removes a class of silent cache misses
that is otherwise almost impossible to notice.
"""

from __future__ import annotations

import re
import unicodedata

# Vietnamese keeps the combining marks, so strip only the formatting/control categories.
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿­"), None)

_WS = re.compile(r"\s+")
# Keep letters, digits and the internal separators that carry meaning in this corpus
# (trip_status, pickup_zone_id, METRIC-TRIP-001, 0,2 km). Splitting those apart would
# destroy the exact identifiers that make a schema question answerable.
_TOKEN = re.compile(r"[0-9A-Za-zÀ-ỹ]+(?:[._-][0-9A-Za-zÀ-ỹ]+)*", re.UNICODE)

# Function words that appear in nearly every Vietnamese sentence. BM25's IDF already
# discounts them; removing them from the BIGRAM stream specifically stops the index
# filling up with "của một", "là các" and similar noise pairs.
_BIGRAM_STOP = frozenset("""
và là của có được cho khi nào thì mà với các những một này đó đây kia ở từ đến theo
trong ngoài trên dưới về bị bởi do nếu hoặc hay nhưng còn đã sẽ đang rất quá cũng
""".split())


def normalise(text: str) -> str:
    """Canonical form used for indexing, querying and prompt assembly.

    NFC, not NFD: composed form is what Vietnamese keyboards and most corpora emit, so it
    is the form a tokenizer has seen most of during training.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_ZERO_WIDTH)
    # Collapse runs of whitespace but keep paragraph structure: a chunk's line breaks
    # carry its heading/body split, and flattening them hurts both reading and reranking.
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def syllables(text: str) -> list[str]:
    """Lowercased syllable stream. The unit BM25 counts."""
    return _TOKEN.findall(normalise(text).lower())


def lexical_terms(text: str) -> list[str]:
    """Unigrams plus adjacent bigrams -- the full term stream for BM25.

    Bigrams are emitted as "a b" with a space so they cannot collide with a unigram
    containing an underscore (trip_status stays one unigram).
    """
    syl = syllables(text)
    terms = list(syl)
    for a, b in zip(syl, syl[1:]):
        if a in _BIGRAM_STOP and b in _BIGRAM_STOP:
            continue
        terms.append(f"{a} {b}")
    return terms
