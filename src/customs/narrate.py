"""What the crew is doing, in words a person would use.

The mission feed is the screen an operator watches while a run works, and
what it showed was the crew's own shorthand -- `mcp search_dashboards
('customs')`, `judge FR: 12 verdicts`. Accurate, and no comfort at all to
someone waiting to find out whether their commercial can air.

So every stage gets a sentence in the present tense saying what is
happening and why it matters, and every line keeps its shorthand
underneath. Nothing is hidden or paraphrased away: the prose is added, the
record is not edited.
"""

# One sentence per agent, in the order a run meets them. Present tense,
# because the feed is watched live; plain nouns, because the reader has a
# commercial to ship and not a pipeline to debug.
STAGE_PROSE = {
    "pipeline": (
        "Getting everything ready",
        "Setting the run up and handing your commercial to the crew."),
    "ingest": (
        "Watching the film for the first time",
        "Cutting your commercial into its own shots, listening to the "
        "soundtrack, and measuring the picture for flashing that could "
        "trouble a photosensitive viewer."),
    "transcription": (
        "Listening to what is said",
        "Writing down the words in each shot, so a claim that is only "
        "spoken is caught as surely as one on screen."),
    "analyst": (
        "Writing down what is actually there",
        "Looking at every shot once and describing what it holds -- a "
        "glass, a hemline, a logo -- in neutral sentences, with no verdict "
        "attached. Every market will argue about these same sentences."),
    "adjudicator": (
        "Asking every market what it thinks",
        "One adjudicator per jurisdiction, all at once, each holding its "
        "own rulebook and looking up the statute behind anything it "
        "objects to."),
    "guard": (
        "Deciding what a machine may not touch",
        "Checking every objection against the rule that raised it. Where a "
        "rule is written on a protected characteristic, this refuses to "
        "edit anything and hands the decision to a person."),
    "publisher": (
        "Putting it all into Grafana",
        "Calling Grafana and making sure everything lands there: the "
        "numbers, the finding detail, the dashboards to read them on and "
        "the alerts that will wake the crew if something needs fixing."),
    "remediator": (
        "Fixing the shots that failed",
        "Working out the cheapest edit that would satisfy the rule, pricing "
        "it, and changing only the seconds that were objected to."),
    "verifier": (
        "Checking the fix actually worked",
        "Watching the changed shots again with the same eye that found the "
        "problem, to confirm it is gone and that nothing new broke."),
}

_FALLBACK = ("Working", "The crew is busy on this run.")


def stage(agent: str) -> tuple[str, str]:
    """(headline, sentence) for one agent's stage of a run."""
    return STAGE_PROSE.get(agent, _FALLBACK)


def headline(agent: str) -> str:
    return stage(agent)[0]


def sentence(agent: str) -> str:
    return stage(agent)[1]
