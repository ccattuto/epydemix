---
id: t05_measles_vermont
title: Measles risk in Vermont across vaccination coverage
timeout_s: 7200
tags: age-structure, contact-layers, coverage-sweep, targeting
source: user-specified
---
Study the potential impact of a measles outbreak in the state of Vermont if vaccination coverage falls
into the 80%–95% range.

SETUP
- Use Vermont's age-structured population and all available contact layers (home, school, work,
  community).
- Measles epidemiology in an SEIR structure: latent period ~12 days, infectious period ~8 days, and a
  transmission rate chosen to give a basic reproduction number typical of measles (R₀ ≈ 15). State the R₀
  you actually obtain and how you determined it.
- MMR coverage is represented as immunity present before the outbreak starts: a covered individual is
  immune at day 0 and plays no further part in transmission.
- Seed the outbreak with 5 imported infectious individuals on 1 January 2027 and run through
  31 December 2027.
- Use 200 stochastic realizations per coverage level.

PART 1 — Coverage sweep
Sweep coverage from 80% to 95% in steps of 1 percentage point. For each level report:
- the probability of a large outbreak (define a threshold, e.g. more than 1,000 cases, and say what you
  used),
- median total cases and the attack rate **among the initially susceptible** (do not count the
  pre-existing immune as cases),
- peak incidence and peak date, with uncertainty.

PART 2 — Imperfect vaccine
The MMR vaccine is not perfect. Repeat the sweep assuming it is 97% effective, so 3% of vaccinated
individuals remain fully susceptible. Does any coverage level in the 80%–95% range still reach herd
immunity?

PART 3 — Age structure and targeting
At 88% coverage (97% vaccine efficacy), report the age-stratified attack rate and the share of total cases
falling in each age group — in particular the 0–4 group, who are largely too young to be fully vaccinated.

Then: if Vermont could run a catch-up campaign targeting a single age group, raising that one group's
coverage to 98% while the rest stay at 88%, which group should it target to minimize total cases? Compare
the candidate age groups, and say whether the best target is the group that carries the most cases or the
one that drives the most transmission.

DELIVERABLES
- A table of coverage → outbreak probability, total cases, attack rate, peak incidence, peak date, for
  both the perfect and the 97%-effective vaccine.
- A table comparing the single-age-group catch-up campaigns.
- A figure: total cases and outbreak probability against coverage, for both vaccine assumptions, with the
  theoretical herd-immunity threshold (1 − 1/R₀) marked as a vertical line.
- A figure: epidemic curves at three representative coverage levels spanning the transition, with
  uncertainty bands.
- A short summary: over what coverage range does outbreak risk collapse, how sharp is that transition, and
  how does the empirical threshold compare with 1 − 1/R₀? What does an 80% versus a 95% Vermont look like
  in absolute numbers, and where should a catch-up campaign go?
