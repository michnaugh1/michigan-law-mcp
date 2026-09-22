import pytest

from mlaw.citations import (
    citation_sort_key, extract_narrative_ranges, extract_xrefs, normalize_citation, normalize_const_mclnumber,
)


@pytest.mark.parametrize(
    "raw,source,canon",
    [
        ("MCL 750.83", "mcl", "MCL 750.83"),
        ("mcl 750.520B(1)(a)", "mcl", "MCL 750.520b"),
        ("750.83", "mcl", "MCL 750.83"),
        ("MCL750.83", "mcl", "MCL 750.83"),
        ("MCL 257.627a et seq.", "mcl", "MCL 257.627a"),
        ("MCR 6.110(B)", "mcr", "MCR 6.110"),
        ("MRE 404(b)", "mre", "MRE 404"),
        ("Const 1963, art 1, § 17", "const", "Const 1963, art 1, § 17"),
        ("Const. 1963, art. I, § 17", "const", "Const 1963, art 1, § 17"),
        ("1963 Const, art IV, § 27", "const", "Const 1963, art 4, § 27"),
        ("Const 1963, Schedule, § 1", "const", "Const 1963, Schedule, § 1"),
        ("Const. 1963, Schedule, § 1", "const", "Const 1963, Schedule, § 1"),
        ("MCL 767A.1", "mcl", "MCL 767A.1"),
        ("mcl 771a.4b(2)", "mcl", "MCL 771A.4b"),
    ],
)
def test_normalize(raw, source, canon):
    c = normalize_citation(raw)
    assert c is not None and (c.source, c.canonical) == (source, canon)


@pytest.mark.parametrize("raw", ["", "hello", "MCL", "section 83", "MCL 750", "0.02", "MCL 0.02"])
def test_normalize_rejects(raw):
    assert normalize_citation(raw) is None


def test_sort_key_natural_order_const():
    # Const citations sort by article number, then section; "Schedule" sorts after every numbered article
    # (matching the real Chapter 1.xml's own division order). Regression test for a real TypeError hit
    # ingesting the real file end to end (2026-09-22): the generic digit-extraction fallback produced
    # different-length tuples for a numbered article ("art 1, § 1" -> 3 digit groups) vs Schedule
    # ("Schedule, § 1" -> 2 digit groups), and comparing them during sort raised int-vs-str.
    cites = [
        "Const 1963, art 12, § 1", "Const 1963, art 1, § 3", "Const 1963, Schedule, § 1",
        "Const 1963, art 1, § 10", "Const 1963, Schedule, § 0",
    ]
    assert sorted(cites, key=citation_sort_key) == [
        "Const 1963, art 1, § 3", "Const 1963, art 1, § 10", "Const 1963, art 12, § 1",
        "Const 1963, Schedule, § 0", "Const 1963, Schedule, § 1",
    ]


def test_sort_key_natural_order():
    cites = ["MCL 768.1", "MCL 767A.1", "MCL 767.96", "MCL 750.83", "MCL 750.10a", "MCL 750.9", "MCL 750.10",
             "MCL 750.520b", "MCL 257.1"]
    assert sorted(cites, key=citation_sort_key) == [
        "MCL 257.1", "MCL 750.9", "MCL 750.10", "MCL 750.10a", "MCL 750.83", "MCL 750.520b",
        "MCL 767.96", "MCL 767A.1", "MCL 768.1",
    ]


def keys(xs):
    return {(x.kind, x.key, x.in_range) for x in xs}


def test_xrefs_single_and_range():
    text = "See section 83, MCL 750.83, and MCL 750.520b to 750.520e; also 1931 PA 328."
    assert keys(extract_xrefs(text)) == {
        ("mcl", "MCL 750.83", False),
        ("mcl", "MCL 750.520b", True),
        ("mcl", "MCL 750.520e", True),
        ("public_act", "1931 PA 328", False),
    }


def test_xrefs_list_and_subdivisions():
    text = "as provided in MCL 750.83(1)(a), 750.84, and 750.85 but not section 5"
    assert keys(extract_xrefs(text)) == {
        ("mcl", "MCL 750.83", False),
        ("mcl", "MCL 750.84", False),
        ("mcl", "MCL 750.85", False),
    }


def test_xrefs_rules_and_constitution_and_self_excluded():
    text = "MCR 6.110 and MRE 404(b); Const 1963, art 1, § 17; MCL 750.83"
    got = keys(extract_xrefs(text, self_citation="MCL 750.83"))
    assert got == {("mcr", "MCR 6.110", False), ("mre", "MRE 404", False), ("const", "Const 1963, art 1, § 17", False)}


def test_xrefs_no_false_positives():
    assert extract_xrefs("The fine is $500.00 or 1.5 times the value; see section 5.") == []


def test_xrefs_rejects_chapter_zero_in_a_citation_list():
    # real text from MCL 777.48 (a sentencing-guideline BAC threshold): "0.02" is a decimal figure, not
    # a continuation of the citation list, but it syntactically matches the MCL token shape (chapter "0",
    # section "02") once swept up by the comma-separated list logic. Chapter 0 doesn't exist in the MCL.
    text = "MCL 257.625, 0.02 grams or more but less than 0.10 grams per 100 milliliters of blood"
    assert keys(extract_xrefs(text)) == {("mcl", "MCL 257.625", False)}


def test_xrefs_cross_chapter_ranges_still_work_after_the_chapter_zero_fix():
    # guard against a regression toward "reject any continuation whose chapter differs from the first
    # token" -- these are all real, legitimate cross-chapter range citations found in Chapters 750/760-777.
    for text, first, second in [
        ("MCL 710.21 to 712B.41", "MCL 710.21", "MCL 712B.41"),
        ("MCL 760.1 to 777.69", "MCL 760.1", "MCL 777.69"),
        ("MCL 764.27a and 769.1", "MCL 764.27a", "MCL 769.1"),
        ("MCL 765.6b and 771.3", "MCL 765.6b", "MCL 771.3"),
        ("MCL 475.1 to 479.42", "MCL 475.1", "MCL 479.42"),
    ]:
        assert {x.key for x in extract_xrefs(text)} == {first, second}


def test_xrefs_public_act_alternate_formats():
    # "Act No. NNN of the Public Acts of YYYY" and the bare "Act NNN of YYYY" are the common way acts get
    # cited in real body/history text, alongside the "YYYY PA NNN" form already covered by _PA_RE.
    assert keys(extract_xrefs("the open meetings act, Act No. 267 of the Public Acts of 1976, being sections")) == {
        ("public_act", "1976 PA 267", False)}
    assert keys(extract_xrefs("was created by Act 368 of the Public Acts of 1978, being Section 333.")) == {
        ("public_act", "1978 PA 368", False)}
    assert keys(extract_xrefs("under Public Act 409 of 2000 will contribute")) == {
        ("public_act", "2000 PA 409", False)}
    assert keys(extract_xrefs("1978 PA 368")) == {("public_act", "1978 PA 368", False)}  # existing format


def test_xrefs_chapter_letters():
    text = "under MCL 767A.1 to 767A.9 and MCL 771A.4"
    assert keys(extract_xrefs(text)) == {
        ("mcl", "MCL 767A.1", True), ("mcl", "MCL 767A.9", True), ("mcl", "MCL 771A.4", False)}


def test_xrefs_being_section_of_the_michigan_compiled_laws():
    # the phrasing actually used in MCL 750.145p and most other statutes
    t = "or section 11b of the social welfare act, being section 400.11b of the Michigan Compiled Laws."
    assert keys(extract_xrefs(t)) == {("mcl", "MCL 400.11b", False)}
    t = "under sections 520b to 520e of the penal code, being sections 750.520b to 750.520e of the Michigan Compiled Laws, and"
    assert keys(extract_xrefs(t)) == {("mcl", "MCL 750.520b", True), ("mcl", "MCL 750.520e", True)}
    # without the closing phrase it is not treated as an MCL citation
    assert extract_xrefs("being section 5.5 of the agreement") == []


def test_bill_summary_separators():
    def keys(t):
        return [(x.key, x.in_range) for x in extract_xrefs(t)]

    assert keys("(MCL 3.1041 & 3.1042)") == [("MCL 3.1041", False), ("MCL 3.1042", False)]
    assert keys("(MCL 460.1 - 460.11)") == [("MCL 460.1", True), ("MCL 460.11", True)]
    assert keys("MCL 750.83-750.85") == [("MCL 750.83", True), ("MCL 750.85", True)]
    assert keys("MCL 750.83 - the offense") == [("MCL 750.83", False)]  # hyphen alone is not a range


def range_keys(rs):
    return {(r.start_citation, r.end_citation, r.status) for r in rs}


def test_narrative_ranges_in_body_repeal_declarations():
    # real text, MCL 333.13607: a whole section whose only job is to repeal a different span
    t = "Sec. 13607.\nSections 13601 to 13606 of Act No. 368 of the Public Acts of 1978, being sections " \
        "333.13601 to 333.13606 of the Michigan Compiled Laws, are repealed effective December 31, 1993."
    assert range_keys(extract_narrative_ranges(t)) == {("MCL 333.13601", "MCL 333.13606", "repealed")}

    # real text, MCL 333.1034: same shape, singular "is repealed", no effective date
    t2 = "Sec. 4.\nAct No. 124 of the Public Acts of 1979, being sections 333.1021 to 333.1024 of the " \
         "Michigan Compiled Laws, is repealed."
    assert range_keys(extract_narrative_ranges(t2)) == {("MCL 333.1021", "MCL 333.1024", "repealed")}

    # an ordinary "being sections ... of the Michigan Compiled Laws" cross-reference is NOT a repeal notice
    t3 = "under sections 520b to 520e of the penal code, being sections 750.520b to 750.520e of the " \
         "Michigan Compiled Laws, and"
    assert extract_narrative_ranges(t3) == []


@pytest.mark.parametrize(
    "raw,canon",
    [
        ("Article I § 3", "Const 1963, art 1, § 3"),
        ("Article IV § 4", "Const 1963, art 4, § 4"),
        ("Article XII § 1", "Const 1963, art 12, § 1"),
        ("Schedule § 1", "Const 1963, Schedule, § 1"),
        ("Schedule § 16", "Const 1963, Schedule, § 16"),
    ],
)
def test_normalize_const_mclnumber(raw, canon):
    # The Constitution's own MCLNumber field shape (Chapter 1.xml) -- distinct from the "Const. 1963, art.
    # I, § 3" prose form normalize_citation/extract_xrefs recognize when the Constitution is referenced
    # from other chapters' text. Confirmed against the real file.
    c = normalize_const_mclnumber(raw)
    assert c is not None and (c.source, c.canonical) == ("const", canon)


def test_normalize_const_mclnumber_rejects_non_section_entries():
    # "Schedule SigBlock" (Chapter 1.xml's ceremonial signature/vote-record block) doesn't have a "§ N"
    # of its own in MCLNumber and is deliberately left unrecognized here -- see mclxml.py.
    assert normalize_const_mclnumber("Schedule SigBlock") is None
    assert normalize_const_mclnumber("37.2102") is None  # an ordinary MCL number, not this shape at all


def test_narrative_ranges_former_mcl_compilers_notes():
    # real Compiler's Notes text, a range: MCL 333.20701
    n1 = "Compiler's Notes: Former MCL 333.20701-333.20773 Expired. 1981, Act 79, Eff. Sept. 30, " \
         "1989;—Repealed, 1990, Act 179, Imd. Eff. July 2, 1990.\nPopular Name: Act 368"
    assert range_keys(extract_narrative_ranges("", n1)) == {("MCL 333.20701", "MCL 333.20773", "expired")}

    # real Compiler's Notes text, a single citation (no range) with a long descriptive clause in between:
    # MCL 769.25, the longest real example found (~216 chars before "was repealed")
    n2 = ("Compiler's Notes: Former MCL 769.25, which pertained to authorized imprisonment in reformatory "
          "at Ionia or Detroit house of correction instead of state prison of any male person convicted "
          "for first time of any offense other than rape, murder, or treason, was repealed by Act 256 of "
          "1964, Eff. Aug. 28, 1964.")
    assert range_keys(extract_narrative_ranges("", n2)) == {("MCL 769.25", "MCL 769.25", "repealed")}

    # a "Former MCL" mention that never says repealed/expired is left alone, not guessed at
    assert extract_narrative_ranges("", "Former MCL 750.1 was renumbered as MCL 750.2.") == []
