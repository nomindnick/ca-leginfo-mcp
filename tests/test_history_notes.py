"""Unit tests for server/history.py against real history-note forms
observed in the built corpus (every docstring example is covered)."""

from server.history import parse_history


def test_simple_stats():
    ph = parse_history(
        "Amended by Stats. 2023, Ch. 260, Sec. 14.   (SB 345)   "
        "Effective January 1, 2024.")
    assert len(ph.events) == 1
    ev = ph.events[0]
    assert (ev.kind, ev.year, ev.chapter, ev.ex_session) == \
        ("chapter", 2023, 260, 0)
    assert ev.role == "operative"
    assert ev.measure_hint == "SB 345"


def test_as_amended_parenthetical_two_citations():
    # GOV 54957.5: the operative 2022 chapter comes AFTER the
    # parenthetical 2021 one in the text; roles and order must reflect it.
    ph = parse_history(
        "Amended (as amended by Stats. 2021, Ch. 615, Sec. 208) by "
        "Stats. 2022, Ch. 971, Sec. 1.   (AB 2647)   "
        "Effective January 1, 2023.")
    assert [(e.year, e.chapter, e.role) for e in ph.events] == [
        (2022, 971, "operative"), (2021, 615, "prior_version")]
    assert ph.events[0].measure_hint == "AB 2647"
    assert ph.events[1].measure_hint is None


def test_extraordinary_session():
    ph = parse_history("Added by Stats. 1946, 1st Ex. Sess., Ch. 114.")
    ev = ph.events[0]
    assert (ev.year, ev.chapter, ev.ex_session) == (1946, 114, 1)


def test_modern_extraordinary_session():
    ph = parse_history(
        "Amended by Stats. 2009, 3rd Ex. Sess., Ch. 17, Sec. 10.   "
        "Effective February 20, 2009.")
    ev = ph.events[0]
    assert (ev.year, ev.chapter, ev.ex_session) == (2009, 17, 3)


def test_initiative():
    ph = parse_history(
        "Added November 4, 2014, by initiative Proposition 47, Sec. 5.")
    assert len(ph.events) == 1
    ev = ph.events[0]
    assert ev.kind == "initiative"
    assert ev.proposition == "47"
    assert ev.date == "November 4, 2014"


def test_cons_resolution_chapter():
    ph = parse_history(
        "Sec. 1.1 added Nov. 8, 2022, by Prop. 1. Res.Ch. 97, 2022.   "
        "Effective December 21, 2022.")
    assert len(ph.events) == 1
    ev = ph.events[0]
    assert ev.kind == "resolution_chapter"
    assert (ev.year, ev.chapter, ev.proposition) == (2022, 97, "1")


def test_cons_resolution_chapter_spelled_out():
    ph = parse_history(
        "Sec. 1 added Nov. 5, 1974, by Proposition 7. "
        "Resolution Chapter 90, 1974.")
    ev = ph.events[0]
    assert ev.kind == "resolution_chapter"
    assert (ev.year, ev.chapter, ev.proposition) == (1974, 90, "7")


def test_cons_resolution_chapter_ex_session_range():
    ph = parse_history(
        "Sec. 10 amended March 2, 2004, by Prop. 58. "
        "Res.Ch. 1, 2003-04 5th Ex. Sess.")
    ev = ph.events[0]
    assert ev.kind == "resolution_chapter"
    assert (ev.year, ev.year_alt, ev.chapter, ev.ex_session) == \
        (2003, 2004, 1, 5)
    assert ev.proposition == "58"


def test_prop_via_stats_not_initiative():
    # "Approved in Proposition 55" alongside a Stats. citation: the
    # chapter citation is the event; no bare-Prop event is invented
    # (the _INITIATIVE branch requires the word "initiative").
    ph = parse_history(
        "Added by Stats. 2002, Ch. 33, Sec. 31.   Approved in "
        "Proposition 55 at the March 2, 2004, election.")
    assert [e.kind for e in ph.events] == ["chapter"]


def test_no_citation():
    ph = parse_history("Repealed and reenacted; see prior law.")
    assert ph.events == []
    assert parse_history(None).events == []


def test_dedupe_identical_citations():
    ph = parse_history(
        "Amended by Stats. 1999, Ch. 78. Amended by Stats. 1999, Ch. 78.")
    assert len(ph.events) == 1


def test_cons_direct_initiative_measure_form():
    # 106 CONS sections use this form: a Prop with NO resolution chapter,
    # because the amendment reached the ballot by voter initiative.
    ph = parse_history(
        "Sec. 28 amended Nov. 4, 2008, by Prop. 9. Initiative measure.")
    assert len(ph.events) == 1
    ev = ph.events[0]
    assert ev.kind == "initiative"
    assert ev.proposition == "9"
    assert ev.date == "Nov. 4, 2008"


def test_multi_citation_cons_note_positional_attribution():
    # Real CONS Art. I Sec. 7 note: each Res.Ch. must take the Prop and
    # date named just before IT, not the note's first Prop/date.
    ph = parse_history(
        "Subdivision (a) amended Nov. 6, 1979, by Prop. 1. Res.Ch. 18, "
        "1979.   Other Source:  Entire Sec. 7 was added Nov. 5, 1974, by "
        "Prop. 7; Res.Ch. 90, 1974.")
    got = [(e.chapter, e.year, e.proposition, e.date) for e in ph.events]
    assert got == [
        (18, 1979, "1", "Nov. 6, 1979"),
        (90, 1974, "7", "Nov. 5, 1974"),
    ]


def test_as_added_parenthetical_variant():
    ph = parse_history(
        "Amended (as added by Stats. 2016, Ch. 50, Sec. 2) by "
        "Stats. 2019, Ch. 143, Sec. 1.   (SB 23)")
    assert [(e.year, e.role) for e in ph.events] == [
        (2019, "operative"), (2016, "prior_version")]


def test_initiative_date_not_stolen_from_stats_citation():
    # When a note mixes a Stats amendment with an initiative origin, the
    # initiative event must carry ITS date, not the note's first date.
    ph = parse_history(
        "Amended by Stats. 2016, Ch. 86, Sec. 1.   (SB 1171)   Effective "
        "January 1, 2017. This section was added June 5, 1990, by "
        "initiative Proposition 115.")
    init = next(e for e in ph.events if e.kind == "initiative")
    assert init.date == "June 5, 1990"
    assert init.proposition == "115"


def test_short_ex_session_form_without_sess():
    """Real notes cite extraordinary sessions both as '1st Ex. Sess.,
    Ch. 9' and the shorter '1st Ex., Ch. 9' (RTC 6362.7's prior-version
    parenthetical carries the short form; eight current-law notes do).
    Both must parse, or the authoritative parenthetical is silently
    dropped and prior-version resolution degrades to a guess."""
    p = parse_history(
        "Amended (as amended by Stats. 1991, 1st Ex., Ch. 9) by "
        "Stats. 1992, Ch. 903, Sec. 1.   (AB 2645)")
    ops = [e for e in p.events if e.role == "operative"]
    priors = [e for e in p.events if e.role == "prior_version"]
    assert (ops[0].year, ops[0].chapter, ops[0].ex_session) == \
        (1992, 903, 0)
    assert (priors[0].year, priors[0].chapter, priors[0].ex_session) == \
        (1991, 9, 1)
