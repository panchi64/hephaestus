"""Synthetic corpora + labeled query/question sets for the eval suite.

Everything here is **fabricated** — none of it is read from ``data/``. There are
two corpora, one per embedding consumer:

- ``MEMORIES`` — ~30 operator memories spanning the categories the agent prompt
  names: preference, project, person, standing constraint, how-they-like-things.
- ``CONVERSATIONS`` — ~15 short prior conversations (a few labeled turns each) on
  distinct topics, seeded through the persistence path so the drainer embeds them.

Each consumer carries two diagnostic retrieval slices, both designed to isolate a
single signal:

- *paraphrase* — the query shares **no tokens** with its gold item, so only the
  dense (embedding) path can hit it. This is the slice that proves embeddings beat
  keyword.
- *rare_token* — an exact identifier/code that embeddings tend to miss, so the
  sparse (keyword) path must carry it.

And two end-to-end question sets per consumer:

- *should_trigger* — answerable only from a stored memory / past conversation;
  each carries a ``gold_fact`` substring for a deterministic grounding check.
- *should_not_trigger* — generic world knowledge; the retrieval tool firing here
  is a false positive / wasted call.

A note on the slice invariant: the paraphrase queries are checked at import time
(:func:`_assert_no_token_overlap`) to genuinely share no alphanumeric token with
their gold item, so the "only dense can hit" property is real, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.ranking import tokens

# --- corpus types ---------------------------------------------------------


@dataclass(frozen=True)
class MemoryItem:
    id: str
    content: str
    category: str  # preference | project | person | constraint | how-they-like


@dataclass(frozen=True)
class ConversationItem:
    id: str
    title: str
    # (user_prompt, assistant_answer) turns — the gold fact lives in a turn.
    turns: list[tuple[str, str]]


@dataclass(frozen=True)
class RetrievalQuery:
    query: str
    gold_ids: list[str]
    slice: str  # paraphrase | rare_token | plain


@dataclass(frozen=True)
class EndToEndQuestion:
    question: str
    should_trigger: bool
    # The substring an answer that actually used retrieval must contain. Empty for
    # should_not_trigger controls (there is no grounded fact to check).
    gold_fact: str = ""
    # A short rubric line for the LLM judge — what "used the memory" looks like.
    rubric: str = ""


@dataclass(frozen=True)
class Corpus:
    """One consumer's full eval input: a corpus, its retrieval queries, its Qs."""

    memories: list[MemoryItem] = field(default_factory=list)
    conversations: list[ConversationItem] = field(default_factory=list)
    queries: list[RetrievalQuery] = field(default_factory=list)
    questions: list[EndToEndQuestion] = field(default_factory=list)


# --- memory corpus --------------------------------------------------------

MEMORIES: list[MemoryItem] = [
    # how-they-like-things / preferences
    MemoryItem("mem-bike", "I commute to the office by bicycle every morning.", "how-they-like"),
    MemoryItem("mem-coffee", "I drink my coffee black, no sugar, no milk.", "preference"),
    MemoryItem("mem-darkmode", "I always want interfaces in dark mode.", "preference"),
    MemoryItem("mem-tabs", "I indent code with spaces, never tabs.", "preference"),
    MemoryItem(
        "mem-brevity", "I prefer short, direct answers over long explanations.", "how-they-like"
    ),
    MemoryItem("mem-metric", "I think in metric units, not imperial.", "preference"),
    MemoryItem("mem-vegan", "I eat a strictly vegan diet.", "constraint"),
    MemoryItem("mem-nopeanut", "I am severely allergic to peanuts.", "constraint"),
    MemoryItem("mem-morning", "I do my best focused work before noon.", "how-they-like"),
    MemoryItem(
        "mem-nocall", "I would rather get a written message than a phone call.", "preference"
    ),
    # projects
    MemoryItem(
        "mem-odysseus", "My main project is Odysseus, a self-hosted AI workspace.", "project"
    ),
    MemoryItem("mem-thesis", "I am writing a thesis on coral reef restoration.", "project"),
    MemoryItem("mem-garden", "I am building a raised-bed vegetable garden this spring.", "project"),
    MemoryItem("mem-novel", "I am drafting a science-fiction novel set on Europa.", "project"),
    MemoryItem("mem-rust", "I am learning the Rust programming language on weekends.", "project"),
    MemoryItem("mem-deadline", "The Odysseus beta is due at the end of September.", "project"),
    # people
    MemoryItem("mem-maria", "My sister Maria is a pediatric nurse in Lisbon.", "person"),
    MemoryItem("mem-sam", "Sam is my co-founder and handles the backend.", "person"),
    MemoryItem("mem-drlee", "Dr. Lee is my dentist; appointments are on Tuesdays.", "person"),
    MemoryItem("mem-mentor", "Priya was my graduate advisor and still mentors me.", "person"),
    MemoryItem("mem-dog", "My dog Biscuit is a twelve-year-old beagle.", "person"),
    # standing constraints
    MemoryItem("mem-budget", "I keep my monthly cloud spend under fifty dollars.", "constraint"),
    MemoryItem(
        "mem-offline",
        "I want everything to keep working with no internet connection.",
        "constraint",
    ),
    MemoryItem("mem-privacy", "I never want my data sent to third-party services.", "constraint"),
    MemoryItem("mem-laptop", "My only machine is a 128 GB Apple Silicon laptop.", "constraint"),
    MemoryItem(
        "mem-quiet", "I keep evenings after nine reserved as quiet, no-work time.", "constraint"
    ),
    # rare-token / identifier memories (sparse must carry these)
    MemoryItem("mem-gate", "My building gate code is 7741.", "how-they-like"),
    MemoryItem("mem-router", "My home router admin URL is 192.168.4.1.", "how-they-like"),
    MemoryItem("mem-license", "My software license key is ODY-X92-Q7K4.", "how-they-like"),
    MemoryItem("mem-flight", "My frequent-flyer number is FF-558213.", "person"),
]

MEMORY_QUERIES: list[RetrievalQuery] = [
    # paraphrase slice — share NO tokens with the gold memory; only dense can hit.
    RetrievalQuery("daily means of reaching their workplace", ["mem-bike"], "paraphrase"),
    RetrievalQuery(
        "what foods must be kept away from them for safety", ["mem-nopeanut"], "paraphrase"
    ),
    RetrievalQuery("when is the operator most productive", ["mem-morning"], "paraphrase"),
    RetrievalQuery("which family member works healthcare", ["mem-maria"], "paraphrase"),
    RetrievalQuery(
        "what flagship software does the operator build", ["mem-odysseus"], "paraphrase"
    ),
    RetrievalQuery("does the operator avoid animal products", ["mem-vegan"], "paraphrase"),
    RetrievalQuery("how should responses be styled for them", ["mem-brevity"], "paraphrase"),
    RetrievalQuery("what hardware does the operator own", ["mem-laptop"], "paraphrase"),
    # rare-token slice — exact codes/ids embeddings miss; sparse must carry.
    RetrievalQuery("7741", ["mem-gate"], "rare_token"),
    RetrievalQuery("192.168.4.1", ["mem-router"], "rare_token"),
    RetrievalQuery("ODY-X92-Q7K4", ["mem-license"], "rare_token"),
    RetrievalQuery("FF-558213", ["mem-flight"], "rare_token"),
    # plain slice — natural overlap, both paths can contribute.
    RetrievalQuery("what programming language is the operator learning", ["mem-rust"], "plain"),
    RetrievalQuery("tell me about the coral reef thesis", ["mem-thesis"], "plain"),
    RetrievalQuery("who is the co-founder", ["mem-sam"], "plain"),
    RetrievalQuery("what is the cloud spend budget", ["mem-budget"], "plain"),
]

MEMORY_QUESTIONS: list[EndToEndQuestion] = [
    EndToEndQuestion(
        "How do I usually get to the office?",
        should_trigger=True,
        gold_fact="bicycle",
        rubric="States the operator commutes by bicycle.",
    ),
    EndToEndQuestion(
        "Is there any food I need to be careful about?",
        should_trigger=True,
        gold_fact="peanut",
        rubric="Warns about the peanut allergy.",
    ),
    EndToEndQuestion(
        "What is my building gate code?",
        should_trigger=True,
        gold_fact="7741",
        rubric="Gives the gate code 7741.",
    ),
    EndToEndQuestion(
        "What is the name of my main project?",
        should_trigger=True,
        gold_fact="Odysseus",
        rubric="Names the project Odysseus.",
    ),
    EndToEndQuestion(
        "Who is my co-founder?",
        should_trigger=True,
        gold_fact="Sam",
        rubric="Identifies Sam as the co-founder.",
    ),
    EndToEndQuestion(
        "What kind of diet do I follow?",
        should_trigger=True,
        gold_fact="vegan",
        rubric="States the operator is vegan.",
    ),
    # should-NOT-trigger controls — generic world knowledge.
    EndToEndQuestion("What is the capital of France?", should_trigger=False),
    EndToEndQuestion("What is the boiling point of water at sea level?", should_trigger=False),
    EndToEndQuestion("Convert 10 kilometers to miles.", should_trigger=False),
    EndToEndQuestion("Who wrote the play Hamlet?", should_trigger=False),
]


# --- conversation corpus --------------------------------------------------

CONVERSATIONS: list[ConversationItem] = [
    ConversationItem(
        "conv-paris",
        "Trip to Paris",
        [
            ("I'm planning a trip to Paris in October.", "Great — October is mild in Paris."),
            ("We settled on the Hotel Marigny near the river.", "Noted, the Hotel Marigny it is."),
        ],
    ),
    ConversationItem(
        "conv-db",
        "Database choice",
        [
            ("Which database should the backend use?", "For this scale, SQLite is a strong fit."),
            ("Let's commit to SQLite then.", "Good — SQLite keeps it single-file and local."),
        ],
    ),
    ConversationItem(
        "conv-marathon",
        "Marathon training",
        [
            ("Help me plan training for my first marathon.", "We can build a sixteen-week plan."),
            (
                "My target finish time is four hours.",
                "A four-hour target sets a roughly nine-minute mile.",
            ),
        ],
    ),
    ConversationItem(
        "conv-recipe",
        "Sourdough recipe",
        [
            ("I want to bake sourdough bread.", "You'll need a mature starter first."),
            ("My starter is named Bubbles.", "Bubbles should be fed twice a day."),
        ],
    ),
    ConversationItem(
        "conv-resume",
        "Resume review",
        [
            ("Can you review my resume?", "Sure, share the highlights."),
            ("I led a team of six engineers at Acme.", "Leading six at Acme is worth featuring."),
        ],
    ),
    ConversationItem(
        "conv-guitar",
        "Learning guitar",
        [
            ("I started learning guitar.", "Start with open chords."),
            ("I'm using a Yamaha FG800 acoustic.", "The FG800 is a solid beginner guitar."),
        ],
    ),
    ConversationItem(
        "conv-tax",
        "Tax question",
        [
            ("I have a question about my taxes.", "Happy to help in general terms."),
            ("I formed an LLC in Delaware last year.", "A Delaware LLC has its own filing steps."),
        ],
    ),
    ConversationItem(
        "conv-plants",
        "Houseplant care",
        [
            ("My houseplants keep dying.", "Often it's overwatering."),
            ("I have a fiddle-leaf fig in the living room.", "Fiddle-leaf figs hate being moved."),
        ],
    ),
    ConversationItem(
        "conv-car",
        "Buying a car",
        [
            ("I'm shopping for a used car.", "What's your budget and use case?"),
            (
                "I test-drove a 2019 Subaru Outback.",
                "The 2019 Outback is reliable for hauling gear.",
            ),
        ],
    ),
    ConversationItem(
        "conv-spanish",
        "Spanish lessons",
        [
            ("I want to learn Spanish.", "Daily practice beats long sessions."),
            (
                "I'm using the app Lingoboost every day.",
                "Consistency with Lingoboost will pay off.",
            ),
        ],
    ),
    ConversationItem(
        "conv-budget",
        "Monthly budget",
        [
            ("Help me set a monthly budget.", "Let's start with fixed costs."),
            ("My rent is 1850 dollars a month.", "At 1850 rent, we size the rest around it."),
        ],
    ),
    ConversationItem(
        "conv-wedding",
        "Wedding planning",
        [
            ("We're planning our wedding.", "Congratulations — what's the date?"),
            ("The venue is the Old Mill on June 14th.", "The Old Mill on June 14th — locked in."),
        ],
    ),
    ConversationItem(
        "conv-server",
        "Server error",
        [
            ("My deploy is failing.", "What's the error?"),
            ("It throws error code E-4471 on startup.", "E-4471 usually means a missing env var."),
        ],
    ),
    ConversationItem(
        "conv-book",
        "Book recommendation",
        [
            ("Recommend me a novel.", "What genres do you like?"),
            ("I just finished and loved Dune.", "If you loved Dune, try Hyperion next."),
        ],
    ),
    ConversationItem(
        "conv-camera",
        "Camera purchase",
        [
            ("I want to get into photography.", "Mirrorless is a great starting point."),
            ("I bought a Sony A6400 last week.", "The A6400 is excellent for beginners."),
        ],
    ),
]

CONVERSATION_QUERIES: list[RetrievalQuery] = [
    # paraphrase slice — share NO tokens with the gold conversation text.
    RetrievalQuery("lodging booked for my autumn getaway abroad", ["conv-paris"], "paraphrase"),
    RetrievalQuery("our chosen persistence engine choice", ["conv-db"], "paraphrase"),
    RetrievalQuery("what musical hobby am starting now", ["conv-guitar"], "paraphrase"),
    RetrievalQuery("headcount once managed professionally", ["conv-resume"], "paraphrase"),
    RetrievalQuery("which sedan model got evaluated recently", ["conv-car"], "paraphrase"),
    RetrievalQuery("new gear acquired capturing images", ["conv-camera"], "paraphrase"),
    RetrievalQuery("recently enjoyed sci-fi story", ["conv-book"], "paraphrase"),
    RetrievalQuery("place chosen to host a marriage", ["conv-wedding"], "paraphrase"),
    # rare-token slice — exact codes/ids; sparse must carry.
    RetrievalQuery("E-4471", ["conv-server"], "rare_token"),
    RetrievalQuery("FG800", ["conv-guitar"], "rare_token"),
    RetrievalQuery("A6400", ["conv-camera"], "rare_token"),
    RetrievalQuery("Lingoboost", ["conv-spanish"], "rare_token"),
    # plain slice.
    RetrievalQuery("what is my marathon target time", ["conv-marathon"], "plain"),
    RetrievalQuery("what did we say about my Delaware LLC", ["conv-tax"], "plain"),
    RetrievalQuery("how much is my monthly rent", ["conv-budget"], "plain"),
    RetrievalQuery("what should I do about my dying houseplants", ["conv-plants"], "plain"),
]

CONVERSATION_QUESTIONS: list[EndToEndQuestion] = [
    EndToEndQuestion(
        "Which hotel did I decide on for my Paris trip?",
        should_trigger=True,
        gold_fact="Marigny",
        rubric="Names the Hotel Marigny.",
    ),
    EndToEndQuestion(
        "What database did we agree to use for the backend?",
        should_trigger=True,
        gold_fact="SQLite",
        rubric="Says SQLite.",
    ),
    EndToEndQuestion(
        "What was the error code my deploy was throwing?",
        should_trigger=True,
        gold_fact="E-4471",
        rubric="Reports the error code E-4471.",
    ),
    EndToEndQuestion(
        "What's my marathon finish-time goal?",
        should_trigger=True,
        gold_fact="four hour",
        rubric="States the four-hour target.",
    ),
    EndToEndQuestion(
        "Which car did I test-drive?",
        should_trigger=True,
        gold_fact="Outback",
        rubric="Names the Subaru Outback.",
    ),
    EndToEndQuestion(
        "What's the venue and date for my wedding?",
        should_trigger=True,
        gold_fact="Old Mill",
        rubric="Names the Old Mill venue.",
    ),
    # should-NOT-trigger controls.
    EndToEndQuestion("What is the speed of light in a vacuum?", should_trigger=False),
    EndToEndQuestion("How many continents are there on Earth?", should_trigger=False),
    EndToEndQuestion("What is the chemical symbol for gold?", should_trigger=False),
    EndToEndQuestion("Translate 'good morning' into German.", should_trigger=False),
]


# --- assembled corpora ----------------------------------------------------


def memory_corpus() -> Corpus:
    return Corpus(memories=MEMORIES, queries=MEMORY_QUERIES, questions=MEMORY_QUESTIONS)


def conversation_corpus() -> Corpus:
    return Corpus(
        conversations=CONVERSATIONS,
        queries=CONVERSATION_QUERIES,
        questions=CONVERSATION_QUESTIONS,
    )


# --- import-time slice invariant ------------------------------------------


def _text_for_ids(ids: list[str], lookup: dict[str, str]) -> str:
    return " ".join(lookup[i] for i in ids if i in lookup)


def _assert_no_token_overlap() -> None:
    """The paraphrase slices must genuinely share no token with their gold item —
    that is the whole property that makes "only dense can hit" a real test, not an
    assumption. Verified at import so a future edit that breaks it fails loudly."""
    mem_text = {m.id: m.content for m in MEMORIES}
    conv_text = {c.id: " ".join(f"{p} {a}" for p, a in c.turns) for c in CONVERSATIONS}
    for queries, lookup in ((MEMORY_QUERIES, mem_text), (CONVERSATION_QUERIES, conv_text)):
        for q in queries:
            if q.slice != "paraphrase":
                continue
            overlap = tokens(q.query) & tokens(_text_for_ids(q.gold_ids, lookup))
            assert not overlap, (
                f"paraphrase query {q.query!r} shares tokens {overlap} with its gold "
                "item — it would no longer isolate the dense path"
            )


_assert_no_token_overlap()
