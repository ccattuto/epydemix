---
id: t01_seirhd_scenarios
title: Custom SEIRHD model, three intervention scenarios
timeout_s: 5400
tags: custom-model, scenarios, figures
source: epydemix_agent_test1.txt
---
MODEL:
Build a custom SEIRHD compartmental model for a respiratory pathogen in a population of 100,000:

S (Susceptible) → E (Exposed) → I (Infectious) → H (Hospitalized) or R (Recovered) → D (Dead, from H) or R (Recovered, from H)

EPIDEMIOLOGY:

- Transmission through contact between S and I (community), and at a reduced rate between S and H (nosocomial).
- Incubation period ~3 days (sigma = 0.33/day).
- Infectious period ~7 days (gamma = 0.14/day). At the end of it, 5% of cases are hospitalized, the rest recover.
- Hospital stay ~10 days (gamma_hosp = 0.1/day). Among hospitalized, 15% die, the rest recover.
- Community transmission rate: beta = 0.45/day. Nosocomial transmission rate: beta_hosp = 0.05/day.

SCENARIOS:
Run three scenarios over 6 months (Sep 1, 2024 → Mar 1, 2025), each with 200 simulations.
Seed the outbreak with a tiny fraction of exposed and infectious individuals.

- Baseline — no intervention.
- Early intervention — on October 1, 2024, transmission drops 40% (beta goes from 0.45 to 0.27) until February 1, 2025.
- Late intervention — same 40% reduction, but starting November 1, 2024 until February 1, 2025.

DELIVERABLES:

- Check that all three model setups are correct before running them.
- Run all three scenarios.
- Report, for each scenario, summary statistics and peak timing for I_total, H_total, D_total.
- Compute for each scenario: attack rate (%), peak hospital census (median + 90% CI), total deaths (median + 90% CI),
  days the hospital census exceeds 500 beds, lives saved vs. baseline.
- Produce three figures: epidemic curves (I_total), hospital capacity (H_total with a 500-bed line),
  cumulative deaths (D_total) — all as scenario overlays with uncertainty bands.
- Summarize: what does a 30-day delay in intervention cost?
