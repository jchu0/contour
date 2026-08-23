"""The check catalogue.

The nine built-in checks ask every registrant the same questions, and several
of those questions are wrong for any given company: NHTSA holds no recalls for
a chipmaker, and a bank's loan-loss provisioning matters more than either.

This is the library of checks a company can be given instead. Each entry is a
pre-written specification — the same `CheckSpec` the executor already runs —
plus the rules that decide whether it applies here at all.

Selection happens in three passes, and the order is the point:

1. **Applicability is decided by code.** SIC 3674 is not a vehicle
   manufacturer and a company that never reports `InventoryNet` cannot have an
   inventory check run against it. These are facts, and asking a model to
   re-derive a fact adds a way to be wrong to something that currently cannot be.
2. **Relevance is ranked by a model**, but only among entries that already
   passed (1). It is choosing what matters most, never what is true.
3. **The roster is settled by a person.** Nothing is pinned until someone
   confirms it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ledger.authored import CheckSpec, SpecError, parse_spec

ANY = "*"


@dataclass(frozen=True)
class Entry:
    """One catalogue check and the conditions under which it can run."""

    spec: CheckSpec
    sectors: tuple[str, ...]      # SIC major-group prefixes, or ANY
    requires: tuple[str, ...]     # us-gaap concepts the company must report
    note: str = ""                # why this sector gets this check

    @property
    def key(self) -> str:
        return self.spec.key

    def applies_to_sector(self, sic: str) -> bool:
        if ANY in self.sectors:
            return True
        sic = (sic or "").strip()
        return any(sic.startswith(prefix) for prefix in self.sectors)

    def missing_concepts(self, available: set[str]) -> list[str]:
        """A requirement naming alternates is met when any one of them is filed."""
        missing = []
        for requirement in self.requires:
            if not any(alt in available for alt in requirement.split("|")):
                missing.append(requirement.split("|")[0])
        return missing


def _entry(sectors, requires, note, **spec) -> Entry:
    return Entry(spec=parse_spec({**spec, "origin": "catalogue"}),
                 sectors=tuple(sectors), requires=tuple(requires), note=note)


# -- the catalogue ---------------------------------------------------------
#
# Thresholds are chosen to be quiet in an ordinary year. A check that fires on
# every company every period is noise wearing the costume of a finding.

CATALOGUE: tuple[Entry, ...] = (
    # ---- applies to any registrant ----
    _entry([ANY], ["Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax"], "Any registrant.",
           key="revenue_swing", title="Revenue swing",
           rationale="A double-digit move in reported revenue reframes every other figure in the filing.",
           kind="xbrl_trend", concept="Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax", direction="either",
           threshold_pct=20, severity="medium"),
    _entry([ANY], ["StockholdersEquity"], "Any registrant.",
           key="equity_erosion", title="Shareholders' equity erosion",
           rationale="A sharp fall in book equity signals losses, buybacks or writedowns that the income statement alone may not show.",
           kind="xbrl_trend", concept="StockholdersEquity", direction="fall",
           threshold_pct=25, severity="high"),
    _entry([ANY], [], "Any registrant.",
           key="going_concern", title="Going-concern language",
           rationale="Substantial-doubt language entering the risk factors is among the most serious disclosures a filer can make.",
           kind="text_watch", section="1A", phrases=["going concern", "substantial doubt"],
           mode="appeared", severity="high"),
    _entry([ANY], [], "Any registrant.",
           key="material_weakness", title="Material-weakness language",
           rationale="A newly disclosed material weakness in internal controls bears on every number in the filing.",
           kind="text_watch", section="1A",
           phrases=["material weakness", "internal control over financial reporting was not effective"],
           mode="appeared", severity="high"),
    _entry([ANY], ["ShareBasedCompensation", "Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax"], "Any registrant.",
           key="sbc_intensity", title="Stock compensation as a share of revenue",
           rationale="Rising stock compensation against flat revenue transfers value from shareholders without touching cash.",
           kind="xbrl_ratio", concept="ShareBasedCompensation", denominator="Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax",
           direction="rise", threshold_pct=25, severity="medium"),
    _entry([ANY], ["Goodwill", "Assets"], "Any registrant.",
           key="goodwill_weight", title="Goodwill as a share of assets",
           rationale="Goodwill growing as a share of the balance sheet raises what an impairment would cost later.",
           kind="xbrl_ratio", concept="Goodwill", denominator="Assets",
           direction="rise", threshold_pct=20, severity="medium"),
    _entry([ANY], ["AccountsReceivableNetCurrent", "Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax"], "Any registrant.",
           key="receivables_stretch", title="Receivables against revenue",
           rationale="Receivables outgrowing revenue means sales are being booked faster than they are collected.",
           kind="xbrl_ratio", concept="AccountsReceivableNetCurrent", denominator="Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax",
           direction="rise", threshold_pct=20, severity="high"),
    _entry([ANY], [], "Any registrant.",
           key="cyber_incident", title="Cybersecurity incident language",
           rationale="A breach entering the risk factors often precedes disclosed costs and regulatory attention.",
           kind="text_watch", section="1A",
           phrases=["cybersecurity incident", "data breach", "ransomware"],
           mode="appeared", severity="medium"),

    # ---- technology hardware, semiconductors (SIC 35xx, 36xx) ----
    _entry(["35", "36"], ["ResearchAndDevelopmentExpense", "Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax"],
           "Hardware and semiconductors compete on R&D.",
           key="rnd_intensity", title="R&D as a share of revenue",
           rationale="A designer competes on research rather than plant. A fall in intensity is a strategy change, not a cost saving.",
           kind="xbrl_ratio", concept="ResearchAndDevelopmentExpense", denominator="Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax",
           direction="either", threshold_pct=15, severity="medium"),
    _entry(["35", "36"], ["InventoryNet", "Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax"],
           "Hardware carries obsolescence risk in inventory.",
           key="inventory_obsolescence", title="Inventory against revenue",
           rationale="Hardware inventory loses value quickly. Inventory building faster than revenue is demand misjudged.",
           kind="xbrl_ratio", concept="InventoryNet", denominator="Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax",
           direction="rise", threshold_pct=25, severity="high"),
    _entry(["35", "36", "37"], [], "Fabless and assembly businesses depend on others' capacity.",
           key="supply_concentration", title="Single-source supply language",
           rationale="New single-source or sole-source language is the company saying its concentration risk changed.",
           kind="text_watch", section="1A",
           phrases=["single source", "sole source", "single supplier"],
           mode="appeared", severity="high"),

    # ---- software and services (SIC 737x) ----
    _entry(["737"], ["ContractWithCustomerLiabilityCurrent", "Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax"],
           "Subscription software bills ahead of recognition.",
           key="deferred_revenue", title="Deferred revenue against revenue",
           rationale="Deferred revenue is booked business not yet recognised. Falling against revenue means the forward book is thinning.",
           kind="xbrl_ratio", concept="ContractWithCustomerLiabilityCurrent",
           denominator="Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax", direction="fall", threshold_pct=15, severity="high"),
    _entry(["737"], ["ResearchAndDevelopmentExpense", "Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax"],
           "Software's principal cost is engineering.",
           key="software_rnd", title="Engineering spend against revenue",
           rationale="For a software business R&D is the product. A sharp fall usually shows up in the roadmap a year later.",
           kind="xbrl_ratio", concept="ResearchAndDevelopmentExpense", denominator="Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax",
           direction="fall", threshold_pct=15, severity="medium"),

    # ---- pharmaceuticals and biotech (SIC 283x) ----
    _entry(["283"], ["ResearchAndDevelopmentExpense"],
           "A drug developer's pipeline is its R&D line.",
           key="pipeline_spend", title="R&D spend",
           rationale="For a developer with few marketed products, the R&D line is the pipeline. A sharp cut is a portfolio decision.",
           kind="xbrl_trend", concept="ResearchAndDevelopmentExpense",
           direction="fall", threshold_pct=20, severity="high"),
    _entry(["283", "384"], [], "Regulated products carry approval risk.",
           key="regulatory_action", title="Regulatory action language",
           rationale="FDA warning-letter or consent-decree language entering the risk factors precedes disclosed remediation cost.",
           kind="text_watch", section="1A",
           phrases=["warning letter", "consent decree", "clinical hold", "recall"],
           mode="appeared", severity="high"),

    # ---- banks and lenders (SIC 60xx, 61xx) ----
    _entry(["60", "61"], ["ProvisionForLoanLeaseAndOtherLosses"],
           "A lender's provisioning is its forward view of credit.",
           key="loan_loss_provision", title="Loan-loss provisioning",
           rationale="Provisioning is management's own forecast of credit deterioration, set before the losses arrive.",
           kind="xbrl_trend", concept="ProvisionForLoanLeaseAndOtherLosses",
           direction="rise", threshold_pct=30, severity="high"),
    _entry(["60", "61"], ["Deposits", "Assets"], "Deposit funding is a bank's stability.",
           key="deposit_base", title="Deposits against assets",
           rationale="Deposits falling as a share of assets means the balance sheet is leaning on costlier or flightier funding.",
           kind="xbrl_ratio", concept="Deposits", denominator="Assets",
           direction="fall", threshold_pct=10, severity="high"),

    # ---- insurance (SIC 63xx) ----
    _entry(["63"], ["LiabilityForClaimsAndClaimsAdjustmentExpense"],
           "An insurer's reserves are its central estimate.",
           key="claims_reserves", title="Claims reserves",
           rationale="Reserves are the insurer's own estimate of what it owes. A large move is a change of view about the book.",
           kind="xbrl_trend", concept="LiabilityForClaimsAndClaimsAdjustmentExpense",
           direction="either", threshold_pct=20, severity="high"),

    # ---- retail, restaurants, consumer (SIC 52-59) ----
    _entry(["52", "53", "54", "55", "56", "57", "58", "59"],
           ["InventoryNet", "Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax"], "Retail's working capital is inventory.",
           key="retail_inventory", title="Inventory against revenue",
           rationale="Inventory rising faster than sales is the earliest sign of markdowns to come.",
           kind="xbrl_ratio", concept="InventoryNet", denominator="Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax",
           direction="rise", threshold_pct=15, severity="high"),
    _entry(["52", "53", "54", "55", "56", "57", "58", "59"],
           ["OperatingLeaseRightOfUseAsset", "Assets"], "Store fleets are leased.",
           key="lease_burden", title="Lease assets against total assets",
           rationale="A growing leased estate is fixed cost that does not fall when sales do.",
           kind="xbrl_ratio", concept="OperatingLeaseRightOfUseAsset", denominator="Assets",
           direction="rise", threshold_pct=15, severity="medium"),

    # ---- motor vehicles (SIC 371x) ----
    _entry(["371"], ["InventoryNet", "Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax"], "Vehicle inventory is capital-intensive.",
           key="vehicle_inventory", title="Vehicle inventory against revenue",
           rationale="Unsold vehicles are among the most expensive inventory to carry, and discounting follows a build.",
           kind="xbrl_ratio", concept="InventoryNet", denominator="Revenues|RevenueFromContractWithCustomerExcludingAssessedTax|RevenueFromContractWithCustomerIncludingAssessedTax",
           direction="rise", threshold_pct=20, severity="high"),
    _entry(["371"], [], "Vehicle makers carry warranty and recall exposure.",
           key="warranty_language", title="Warranty and recall language",
           rationale="New warranty or recall language in the risk factors usually precedes a charge.",
           kind="text_watch", section="1A",
           phrases=["warranty claims", "recall campaign", "field action"],
           mode="appeared", severity="medium"),

    # ---- oil, gas, mining (SIC 13xx, 29xx, 10xx) ----
    _entry(["13", "29", "10"], ["PropertyPlantAndEquipmentNet", "Assets"],
           "Extractives are built on reserves and plant.",
           key="ppe_weight", title="Plant and equipment against assets",
           rationale="An extractive business is its asset base; a sharp move means acquisition, disposal or writedown.",
           kind="xbrl_ratio", concept="PropertyPlantAndEquipmentNet", denominator="Assets",
           direction="either", threshold_pct=15, severity="medium"),
    _entry(["13", "29", "10", "49"], [], "Extractives and utilities carry environmental exposure.",
           key="environmental_language", title="Environmental liability language",
           rationale="New remediation or environmental-liability language typically precedes a disclosed provision.",
           kind="text_watch", section="1A",
           phrases=["remediation", "environmental liability", "superfund"],
           mode="appeared", severity="medium"),

    # ---- airlines and transport (SIC 45xx, 47xx) ----
    _entry(["45", "47"], ["LongTermDebtNoncurrent", "Assets"],
           "Fleet-based transport runs on leverage.",
           key="transport_leverage", title="Long-term debt against assets",
           rationale="Fleets are debt-financed, and leverage rising into a downturn is what turns a bad year into a restructuring.",
           kind="xbrl_ratio", concept="LongTermDebtNoncurrent", denominator="Assets",
           direction="rise", threshold_pct=20, severity="high"),
)


def by_key(key: str) -> Entry | None:
    return next((e for e in CATALOGUE if e.key == key), None)


def applicable(sic: str, concepts: set[str]) -> tuple[list[Entry], list[tuple[Entry, str]]]:
    """(eligible, excluded_with_reason). Entirely deterministic.

    Nothing here is a judgement call: either the company files in this sector
    and reports these concepts, or it does not.
    """
    eligible, excluded = [], []
    for entry in CATALOGUE:
        if not entry.applies_to_sector(sic):
            excluded.append((entry, f"written for other sectors (this filer is SIC {sic or '?'})"))
            continue
        missing = entry.missing_concepts(concepts)
        if missing:
            excluded.append((entry, f"does not report {', '.join(missing[:2])}"))
            continue
        eligible.append(entry)
    return eligible, excluded


def sectors_covered() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in CATALOGUE:
        for s in entry.sectors:
            counts[s] = counts.get(s, 0) + 1
    return counts


# -- pass 2: relevance ranking ---------------------------------------------

RANK_SYSTEM = """You choose which checks a company should be given.

You are handed a shortlist that has ALREADY been filtered: every check on it is \
known to apply to this company's sector and to use figures the company actually \
reports. You cannot add to the list and you cannot decide what is true.

Pick the ones that matter most for THIS company and put them in order. Prefer \
checks that suit its specific business over ones that would suit any filer. \
Recommend between 4 and 8. Say briefly why each is worth running here.

Anything you leave out stays available for the user to add by hand, so leaving \
out a weak check costs nothing."""

RANK_TOOL = {
    "name": "rank_checks",
    "description": "Return the recommended checks in priority order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "recommended": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "a key from the shortlist"},
                        "why": {"type": "string",
                                "description": "one sentence on why it suits this company"},
                    },
                    "required": ["key", "why"],
                },
            }
        },
        "required": ["recommended"],
    },
}


@dataclass(frozen=True)
class Recommendation:
    keys: tuple[str, ...] = ()
    why: dict | None = None
    model: str | None = None
    reason: str | None = None       # set when the model could not be consulted

    @property
    def from_model(self) -> bool:
        return self.model is not None


def default_order(entries: list[Entry]) -> list[Entry]:
    """The fallback ranking: severity first, sector-specific before universal.

    Used whenever the model cannot be reached, so the wizard always has a
    sensible pre-selection rather than an empty list.
    """
    weight = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        entries,
        key=lambda e: (weight.get(e.spec.severity, 1), ANY in e.sectors, e.key),
    )


def recommend(ticker: str, company: str, sic: str, sic_label: str,
              entries: list[Entry], *, model: str | None = None) -> Recommendation:
    """Rank the shortlist. Falls back to `default_order` without credentials."""
    import os

    from ledger.authored import MODEL

    model = model or MODEL
    if not entries:
        return Recommendation(reason="nothing applies to this company")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return Recommendation(
            keys=tuple(e.key for e in default_order(entries)[:6]),
            reason="ordered by severity — no Anthropic credentials to rank with",
        )
    try:
        import anthropic
    except ImportError:
        return Recommendation(keys=tuple(e.key for e in default_order(entries)[:6]),
                              reason="the anthropic package is not installed")

    shortlist = "\n".join(
        f"- {e.key}: {e.spec.title} — {e.spec.rationale}" for e in entries
    )
    prompt = (f"Company: {company} ({ticker.upper()})\n"
              f"Industry: {sic_label} (SIC {sic})\n\n"
              f"Shortlist:\n{shortlist}")
    try:
        response = anthropic.Anthropic().messages.create(
            model=model, max_tokens=2000, system=RANK_SYSTEM,
            thinking={"type": "adaptive"}, tools=[RANK_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — ranking must never block the wizard
        return Recommendation(keys=tuple(e.key for e in default_order(entries)[:6]),
                              reason=f"ordered by severity — ranking failed "
                                     f"({type(exc).__name__})")

    payload = None
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == RANK_TOOL["name"]:
            payload = (block.input or {}).get("recommended")
            break
    if not payload:
        return Recommendation(keys=tuple(e.key for e in default_order(entries)[:6]),
                              reason="ordered by severity — the model returned no ranking")

    valid = {e.key for e in entries}
    keys, why = [], {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key", "")).strip()
        if key in valid and key not in keys:
            keys.append(key)
            why[key] = str(row.get("why", "")).strip()[:220]
    if not keys:
        return Recommendation(keys=tuple(e.key for e in default_order(entries)[:6]),
                              reason="ordered by severity — the model named no check on the shortlist")
    return Recommendation(keys=tuple(keys), why=why, model=model)
