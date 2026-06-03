"""Source-of-truth data for the Qatar (QSE) per-stock knowledge base.

This module holds the QSE taxonomy and, per ticker, a *time-aware* profile: name
changes, the accounting framework by era, expected business/geography segments,
foreign subsidiaries (with their currencies and the year they joined), and a dated
event timeline (acquisitions, regulatory regimes, name changes). `qatar/__init__.py`
builds full profiles from this and exposes the public API.

Authoring rule: only encode facts we are confident about. Depth is greatest for the
majors; every one of the 55 tickers still resolves to a complete, valid profile.
"""
from __future__ import annotations

# ── QSE taxonomy (moved here so qatar/ is the single source of truth) ─────────
# group -> [(sub_sector, extraction archetype)]   (order preserved for the UI)
QSE_TAXONOMY: dict[str, list[tuple[str, str]]] = {
    "Banks & Financial Services": [
        ("Commercial Bank", "conventional_bank"),
        ("Islamic Bank", "islamic_bank"),
        ("Brokerage", "other"),
        ("Joint Investment", "other"),
        ("Financial Holding", "other"),
        ("Islamic Financial Services", "islamic_bank"),
    ],
    "Insurance": [
        ("Conventional Insurance", "insurance"),
        ("Takaful Insurance", "insurance"),
        ("Reinsurance", "insurance"),
        ("Life & Medical Insurance", "insurance"),
    ],
    "Real Estate": [
        ("Diversified Real Estate", "other"),
        ("Property Development", "other"),
        ("Real Estate Holding", "other"),
    ],
    "Industrials": [
        ("Petrochemicals", "industrial"),
        ("Aluminium", "industrial"),
        ("Utilities", "other"),
        ("Cement & Building Materials", "industrial"),
        ("Oil & Gas Services", "industrial"),
        ("Diversified Manufacturing", "industrial"),
        ("Industrial Holding", "other"),
        ("Diversified Conglomerate", "other"),
        ("Diversified Holding", "other"),
        ("Trading & Distribution", "industrial"),
    ],
    "Consumer Goods & Services": [
        ("Food & Beverages", "industrial"),
        ("Food Production", "industrial"),
        ("Supermarkets & Retail", "industrial"),
        ("Fuel Retail", "industrial"),
        ("Technology Distribution", "industrial"),
        ("Medical Devices", "industrial"),
        ("Healthcare Services", "other"),
        ("Education", "other"),
        ("Media & Entertainment", "other"),
    ],
    "Telecom & Technology": [
        ("Telecom Operator", "other"),
        ("IT Services", "other"),
    ],
    "Transport": [
        ("Shipping & Marine", "other"),
        ("Warehousing & Logistics", "other"),
        ("LNG Shipping", "other"),
    ],
    "Energy": [("Energy", "industrial")],
    "Other": [("Other", "other")],
}

# ticker -> sub-sector label (the 55 QSE listings)
SYMBOL_SUBSECTOR: dict[str, str] = {
    "QNBK": "Commercial Bank", "CBQK": "Commercial Bank", "DHBK": "Commercial Bank",
    "ABQK": "Commercial Bank", "KCBK": "Commercial Bank",
    "QIBK": "Islamic Bank", "QIIK": "Islamic Bank", "MARK": "Islamic Bank",
    "DUBK": "Islamic Bank", "QFBQ": "Islamic Bank",
    "DBIS": "Brokerage", "QOIS": "Joint Investment", "NLCS": "Financial Holding",
    "IHGS": "Islamic Financial Services",
    "QATI": "Conventional Insurance", "DOHI": "Conventional Insurance",
    "QGRI": "Reinsurance", "QLMI": "Life & Medical Insurance",
    "AKHI": "Takaful Insurance", "QISI": "Takaful Insurance", "BEMA": "Takaful Insurance",
    "UDCD": "Diversified Real Estate", "BRES": "Property Development",
    "ERES": "Real Estate Holding", "MRDS": "Property Development",
    "IQCD": "Petrochemicals", "MPHC": "Petrochemicals", "QAMC": "Aluminium",
    "QEWS": "Utilities", "QNCD": "Cement & Building Materials", "GISS": "Oil & Gas Services",
    "QIMD": "Diversified Manufacturing", "QIGD": "Industrial Holding",
    "AHCS": "Diversified Conglomerate", "IGRD": "Diversified Holding",
    "MKDM": "Diversified Holding", "MHAR": "Industrial Holding", "SIIS": "Trading & Distribution",
    "ZHCD": "Food & Beverages", "WDAM": "Food Production", "MERS": "Supermarkets & Retail",
    "BLDN": "Food Production", "QFLS": "Fuel Retail", "MCCS": "Technology Distribution",
    "QGMD": "Medical Devices", "MCGS": "Healthcare Services", "FALH": "Education",
    "QCFS": "Media & Entertainment",
    "ORDS": "Telecom Operator", "VFQS": "Telecom Operator", "MEZA": "IT Services",
    "TQES": "IT Services",
    "QNNS": "Shipping & Marine", "GWCS": "Warehousing & Logistics", "QGTS": "LNG Shipping",
}

# sub-sector label -> one of the 5 extraction archetypes
SUBSECTOR_TO_ARCHETYPE: dict[str, str] = {
    sub: arch for group in QSE_TAXONOMY.values() for (sub, arch) in group
}

# ── KPIs to watch, by archetype (canonical KPI_* codes) ──────────────────────
WATCH_KPIS: dict[str, list[str]] = {
    "conventional_bank": ["KPI_CAR", "KPI_NPL", "KPI_COVERAGE", "KPI_COST_INCOME",
                          "KPI_NIM", "KPI_LDR", "KPI_ROE", "KPI_ROA"],
    "islamic_bank": ["KPI_CAR", "KPI_NPL", "KPI_COVERAGE", "KPI_COST_INCOME",
                     "KPI_LDR", "KPI_ROE", "KPI_ROA"],
    "insurance": ["KPI_LOSS_RATIO", "KPI_EXPENSE_RATIO", "KPI_COMBINED",
                  "KPI_GWP", "KPI_NET_PREMIUMS", "KPI_ROE"],
    "industrial": ["KPI_ROE", "KPI_ROA"],
    "other": ["KPI_ROE", "KPI_ROA"],
}

# ── Full company names (best-effort, all 55) ─────────────────────────────────
COMPANY_NAMES: dict[str, str] = {
    "QNBK": "Qatar National Bank (QNB)", "CBQK": "The Commercial Bank (Q.P.S.C.)",
    "DHBK": "Doha Bank", "ABQK": "Ahli Bank (Qatar)",
    "KCBK": "Al Khalij Commercial Bank (al khaliji)",
    "QIBK": "Qatar Islamic Bank", "QIIK": "Qatar International Islamic Bank",
    "MARK": "Masraf Al Rayan", "DUBK": "Dukhan Bank", "QFBQ": "Lesha Bank",
    "DBIS": "Dlala Brokerage and Investment Holding", "QOIS": "Qatar Oman Investment Company",
    "NLCS": "National Leasing Holding (Alijarah)", "IHGS": "Inma Holding",
    "QATI": "Qatar Insurance Company (QIC)", "DOHI": "Doha Insurance Group",
    "QGRI": "Qatar General Insurance & Reinsurance", "QLMI": "QLM Life & Medical Insurance",
    "AKHI": "Al Khaleej Takaful Insurance", "QISI": "Qatar Islamic Insurance Group",
    "BEMA": "Damaan Islamic Insurance Company (Beema)",
    "UDCD": "United Development Company", "BRES": "Barwa Real Estate",
    "ERES": "Ezdan Holding Group", "MRDS": "Mazaya Qatar Real Estate Development",
    "IQCD": "Industries Qatar", "MPHC": "Mesaieed Petrochemical Holding Company",
    "QAMC": "Qatar Aluminium Manufacturing Company (Qamco)",
    "QEWS": "Qatar Electricity & Water Company", "QNCD": "Qatar National Cement Company",
    "GISS": "Gulf International Services", "QIMD": "Qatar Industrial Manufacturing Company",
    "QIGD": "Qatari Investors Group", "AHCS": "Aamal Company",
    "IGRD": "Investment Holding Group", "MKDM": "Mekdam Holding Group",
    "MHAR": "Al Mahhar Holding", "SIIS": "Salam International Investment",
    "ZHCD": "Zad Holding Company", "WDAM": "Widam Food Company",
    "MERS": "Al Meera Consumer Goods Company", "BLDN": "Baladna",
    "QFLS": "Qatar Fuel Company (WOQOD)", "MCCS": "Mannai Corporation",
    "QGMD": "Qatar German Company for Medical Devices", "MCGS": "Medicare Group",
    "FALH": "Al Faleh Educational Holding", "QCFS": "Qatar Cinema & Film Distribution",
    "ORDS": "Ooredoo", "VFQS": "Vodafone Qatar", "MEZA": "MEEZA", "TQES": "Estithmar Holding",
    "QNNS": "Qatar Navigation (Milaha)", "GWCS": "Gulf Warehousing Company (GWC)",
    "QGTS": "Qatar Gas Transport Company (Nakilat)",
}

# Qatar-wide regulatory events that apply to all banks (merged into each bank profile).
_BASEL_III = {"year": 2014, "type": "regulation", "title": "Basel III (QCB)",
              "effect": "Capital adequacy reported under Basel III; CET1/AT1/Tier-2 split; "
                        "AT1 instruments (often sukuk) shown in equity."}
_IFRS9 = {"year": 2018, "type": "accounting", "title": "IFRS 9 (expected credit losses)",
          "effect": "Loan-loss provisioning moves to forward-looking ECL with Stage 1/2/3; "
                    "watch ECL coverage and staging migration."}

# ── Per-ticker enrichment (majors hand-authored) ─────────────────────────────
# Only high-confidence facts. Each profile still validates without these.
ENRICH: dict[str, dict] = {
    "QNBK": {
        "reporting_currency": "QAR",
        "segments_expected": {"by_geography": ["Qatar", "Egypt", "Turkey", "Other GCC",
                                               "Europe", "Asia"],
                              "by_business": ["Corporate", "Retail", "Treasury", "International"]},
        "subsidiaries": [
            {"name": "QNB AlAhli", "country": "Egypt", "currency": "EGP", "from": 2013},
            {"name": "QNB Finansbank", "country": "Turkey", "currency": "TRY", "from": 2016},
            {"name": "QNB Indonesia", "country": "Indonesia", "currency": "IDR", "from": 2011},
        ],
        "events": [
            {"year": 2013, "type": "acquisition", "title": "Acquired NSGB → QNB AlAhli (Egypt)",
             "effect": "Adds the Egypt geography segment; EGP translation exposure from 2013."},
            {"year": 2016, "type": "acquisition", "title": "Acquired Finansbank → QNB Finansbank (Turkey)",
             "effect": "Adds Turkey; large book; significant TRY translation effects thereafter."},
            {"year": 2022, "type": "accounting", "title": "IAS 29 hyperinflation (Turkey)",
             "effect": "Turkish operations reported under hyperinflation accounting from 2022."},
        ],
        "peers": ["CBQK", "DHBK", "ABQK", "KCBK"],
        "accounting_quirks": ["AT1 capital instruments presented in equity",
                              "IAS 29 hyperinflation for Turkey from 2022"],
    },
    "CBQK": {
        "segments_expected": {"by_geography": ["Qatar", "Turkey", "UAE", "Oman"],
                              "by_business": ["Wholesale", "Retail", "Treasury"]},
        "subsidiaries": [
            {"name": "Alternatif Bank", "country": "Turkey", "currency": "TRY", "from": 2013},
            {"name": "National Bank of Oman (associate)", "country": "Oman", "currency": "OMR"},
            {"name": "United Arab Bank (associate)", "country": "UAE", "currency": "AED"},
        ],
        "events": [
            {"year": 2013, "type": "acquisition", "title": "Acquired majority of Alternatif Bank (Turkey)",
             "effect": "Adds Turkey; TRY exposure (full ownership reached 2016)."},
            {"year": 2022, "type": "accounting", "title": "IAS 29 hyperinflation (Turkey)",
             "effect": "Alternatif Bank reported under hyperinflation accounting from 2022."},
        ],
        "peers": ["QNBK", "DHBK", "ABQK"],
    },
    "DHBK": {
        "segments_expected": {"by_geography": ["Qatar", "UAE", "Kuwait", "India"],
                              "by_business": ["Wholesale", "Retail", "International", "Treasury"]},
        "peers": ["QNBK", "CBQK", "ABQK"],
    },
    "QIBK": {
        "framework_timeline": [{"framework": "IFRS as adopted by QCB (Islamic)", "from": None}],
        "segments_expected": {"by_geography": ["Qatar", "UK", "Lebanon"],
                              "by_business": ["Corporate", "Retail", "Treasury",
                                              "Investment / Sukuk"]},
        "peers": ["QIIK", "MARK", "DUBK"],
        "accounting_quirks": ["No interest income — profit on Islamic financing",
                              "Sukuk and quasi-equity (unrestricted investment accounts)"],
    },
    "QIIK": {"peers": ["QIBK", "MARK", "DUBK"]},
    "MARK": {
        "segments_expected": {"by_geography": ["Qatar"],
                              "by_business": ["Corporate", "Retail", "Treasury / Sukuk"]},
        "names": [{"name": "Masraf Al Rayan", "from": None, "to": None}],
        "events": [
            {"year": 2021, "type": "merger", "title": "Merger with Al Khalij Commercial Bank (al khaliji)",
             "effect": "Absorbed al khaliji (KCBK); balance sheet roughly doubled from 2021."},
        ],
        "peers": ["QIBK", "QIIK", "DUBK"],
    },
    "DUBK": {
        "names": [{"name": "Barwa Bank", "from": None, "to": 2020},
                  {"name": "Dukhan Bank", "from": 2020, "to": None}],
        "events": [
            {"year": 2019, "type": "merger", "title": "Barwa Bank / International Bank of Qatar merger",
             "effect": "Barwa Bank merged with IBQ (2019); rebranded Dukhan Bank (2020); listed 2023."},
        ],
        "peers": ["QIBK", "QIIK", "MARK"],
    },
    "QFBQ": {
        "names": [{"name": "Qatar First Bank", "from": None, "to": 2022},
                  {"name": "Lesha Bank", "from": 2022, "to": None}],
        "events": [
            {"year": 2022, "type": "rename", "title": "Qatar First Bank → Lesha Bank",
             "effect": "Shari'a-compliant investment bank; rebranded Lesha Bank in 2022."},
        ],
    },
    "IHGS": {
        "names": [{"name": "Islamic Holding Group", "from": None, "to": 2022},
                  {"name": "Inma Holding", "from": 2022, "to": None}],
    },
    "KCBK": {
        "names": [{"name": "Al Khalij Commercial Bank (al khaliji)", "from": None, "to": 2021}],
        "events": [
            {"year": 2021, "type": "merger", "title": "Merged into Masraf Al Rayan (MARK)",
             "effect": "al khaliji merged into Masraf Al Rayan in 2021 and was delisted."},
        ],
        "peers": ["MARK", "QNBK", "CBQK"],
    },
    "ORDS": {
        "segments_expected": {
            "by_geography": ["Qatar", "Indonesia", "Iraq", "Oman", "Kuwait",
                             "Tunisia", "Algeria", "Maldives", "Palestine"],
            "by_business": ["Mobile", "Fixed", "Wholesale"]},
        "subsidiaries": [
            {"name": "Indosat Ooredoo Hutchison", "country": "Indonesia", "currency": "IDR"},
            {"name": "Asiacell", "country": "Iraq", "currency": "IQD"},
            {"name": "Ooredoo Oman", "country": "Oman", "currency": "OMR"},
            {"name": "Ooredoo Kuwait", "country": "Kuwait", "currency": "KWD"},
            {"name": "Ooredoo Tunisia", "country": "Tunisia", "currency": "TND"},
            {"name": "Ooredoo Algeria", "country": "Algeria", "currency": "DZD"},
        ],
        "events": [
            {"year": 2022, "type": "merger", "title": "Indosat–Hutchison merger (Indonesia)",
             "effect": "Indonesian unit became Indosat Ooredoo Hutchison in 2022; changes consolidation."},
            {"year": 2022, "type": "divestment", "title": "Exit Myanmar",
             "effect": "Sold Ooredoo Myanmar in 2022; geography removed thereafter."},
        ],
        "peers": ["VFQS"],
        "accounting_quirks": ["Multi-currency geographic segments; FX translation is material",
                              "Watch net debt / EBITDA and EBITDA margin by country"],
    },
    "VFQS": {"segments_expected": {"by_geography": ["Qatar"],
                                   "by_business": ["Mobile", "Fixed"]},
             "peers": ["ORDS"]},
    "IQCD": {
        "segments_expected": {"by_geography": ["Qatar"],
                              "by_business": ["Petrochemicals", "Fertilizers", "Steel"]},
        "events": [
            {"year": 2022, "type": "restructuring", "title": "Qatar Steel operations wound down",
             "effect": "Steel segment scaled back from 2022; mix shifts to petrochemicals/fertilizers."},
        ],
        "peers": ["MPHC", "QAMC"],
        "accounting_quirks": ["Largely equity-accounted JVs (QAPCO, QAFCO, QASCO)"],
    },
    "MPHC": {"segments_expected": {"by_geography": ["Qatar"],
                                   "by_business": ["Petrochemicals (olefins)", "Plastics", "Alpha olefins"]},
             "peers": ["IQCD", "QAMC"],
             "accounting_quirks": ["Holding company of petrochemical JVs (equity-accounted)"]},
    "QAMC": {"segments_expected": {"by_geography": ["Qatar"], "by_business": ["Aluminium (Qatalum JV)"]},
             "peers": ["IQCD", "MPHC"],
             "accounting_quirks": ["50% JV in Qatalum, equity-accounted"]},
    "QEWS": {"segments_expected": {"by_geography": ["Qatar", "International"],
                                   "by_business": ["Power generation", "Water desalination"]},
             "accounting_quirks": ["Several power/water JVs equity-accounted (Ras Girtas, etc.)"]},
    "QATI": {
        "segments_expected": {
            "by_geography": ["Qatar / MENA", "United Kingdom", "Europe", "Bermuda"],
            "by_business": ["Domestic / MENA insurance", "International (Antares, Qatar Re)"]},
        "subsidiaries": [
            {"name": "Antares (Lloyd's)", "country": "United Kingdom", "currency": "GBP"},
            {"name": "QIC Europe", "country": "Europe", "currency": "EUR"},
        ],
        "peers": ["DOHI", "QGRI"],
        "accounting_quirks": ["Large international book; multi-currency; watch combined ratio",
                              "IFRS 17 insurance contracts from 2023"],
    },
    "UDCD": {"segments_expected": {"by_geography": ["Qatar"],
                                   "by_business": ["Real estate", "District cooling (Qatar Cool)",
                                                   "Hospitality", "Infrastructure"]},
             "accounting_quirks": ["The Pearl Island master developer"]},
    "QGTS": {"reporting_currency": "QAR",
             "segments_expected": {"by_geography": ["International (global LNG fleet)"],
                                   "by_business": ["LNG shipping", "LPG / product shipping",
                                                   "Vessel JVs"]},
             "accounting_quirks": ["USD-linked charter revenue; many vessel-owning JVs",
                                   "Hedging of interest-rate/FX on long-term charters"]},
    "QNNS": {"names": [{"name": "Qatar Navigation (Milaha)", "from": None, "to": None}],
             "segments_expected": {"by_geography": ["Qatar", "International"],
                                   "by_business": ["Maritime & Logistics", "Gas & Petrochem shipping",
                                                   "Offshore", "Trading", "Capital (investments)"]}},
    "GISS": {"segments_expected": {"by_geography": ["Qatar", "International"],
                                   "by_business": ["Drilling", "Aviation (Gulf Helicopters)",
                                                   "Insurance (Al Koot)", "Catering"]},
             "accounting_quirks": ["Conglomerate of oilfield-services segments"]},
    "BLDN": {"segments_expected": {"by_geography": ["Qatar"],
                                   "by_business": ["Dairy", "Juice", "Livestock"]}},
    "MERS": {"segments_expected": {"by_geography": ["Qatar"],
                                   "by_business": ["Retail", "Wholesale", "Logistics"]}},
    "QFLS": {"segments_expected": {"by_geography": ["Qatar"],
                                   "by_business": ["Fuel retail", "Non-fuel retail (Sidra)",
                                                   "Bunkering", "Transport"]}},
    "AHCS": {"segments_expected": {"by_geography": ["Qatar"],
                                   "by_business": ["Industrial Manufacturing", "Trading & Distribution",
                                                   "Property", "Managed Services"]}},
}
