---
id: t04_school_closure_ny
title: Optimal timing of a three-week school closure (New York State)
timeout_s: 7200
tags: age-structure, contact-layers, intervention-timing, parameter-sweep
source: user-specified
---
Study the effect of a three-week school closure on a regular influenza season in New York State.

SETUP
- Use New York State's age-structured population and all available contact layers (home, school, work,
  community).
- Influenza-like epidemiology, seeded with a small number of infectious individuals at the start of the
  season.
- The season runs 1 October 2026 → 31 May 2027.
- Tune the model so that, with no intervention, the epidemic peaks around 15 February 2027. Report the
  peak date you actually achieve and how you got there.
- Use 100 stochastic realizations per configuration.

INTERVENTION
A school closure removes all school-layer contacts for 21 consecutive days. Nothing else changes — the
other contact layers are untouched.

QUESTION
Where in the season should those three weeks be placed to minimize the final attack rate, and how much
does the best placement actually buy you?

- Sweep the closure start date weekly from 1 December 2026 through 1 April 2027, running the full season
  for each start date.
- Also run the unmitigated season as the reference.
- For each start date compute the final attack rate (cumulative infections as a percentage of the
  population), the peak incidence, and the peak date.

DELIVERABLES
- A table of closure start date → final attack rate, peak incidence, peak date.
- The optimal start date, and the relative reduction in attack rate it achieves versus no closure.
- How sensitive the benefit is to timing: how much of the benefit is lost by starting two weeks too early
  or two weeks too late, and the range of start dates that come within 10% of the optimum's benefit.
- Two figures:
  - Final attack rate vs. closure start date, with the unmitigated attack rate as a horizontal reference
    line and the unmitigated peak date marked.
  - Epidemic curves over the season for three cases overlaid — no closure, optimally timed closure, and a
    closure starting one month after the optimum — with the closure windows shaded and uncertainty bands.
- A short summary answering the question directly: what is the ideal placement, what relative impact does
  it achieve, and briefly why the optimum sits where it does.
