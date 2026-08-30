---
id: t03_calibration_projection
title: Calibrate, then project baseline vs intervention
timeout_s: 5400
tags: calibration, projection, scenario-comparison, figures
source: epydemix_agent_test4.txt
---
STEP 1: Generate synthetic observed data and calibrate

Simulate a synthetic outbreak (afterwards, we will calibrate against it).

Run a forward SIR simulation with these "true" parameters:
- Population: 500,000
- transmission_rate: 0.12
- recovery_rate: 0.04
- Period: Jan 1 – Mar 31, 2026 (~90 days)
- Initial conditions: S = 0.9999, I = 0.0001, R = 0.0
- 1 simulation

Extract Infected_total and save as observed.csv (columns: date, cases).

Run a calibration that:
- Fixes recovery_rate = 0.04.
- Places a uniform prior on transmission_rate over [0.03, 0.4].
- Uses the observed.csv generated above, targets Infected_total, uses RMSE distance.
- Uses the SMC strategy with 200 particles and 5 generations.

Validate and run the calibration. Inspect the posterior — confirm that the estimated transmission_rate is
reasonably close to 0.12.

STEP 2: BASELINE projection (no intervention)

Produce a projection that extends the simulation 3 months beyond the calibration period:
- Starting from the simulation parameters above, change only the simulation end date to 30 June 2026.
- 200 posterior samples.

Inspect the results: what are the projected peak date and magnitude for Infected_total? Get quantiles
(0.05, 0.5, 0.95).

STEP 3: Intervention projection

Produce a second projection (intervention):
- Extend to 30 June 2026 (same as baseline).
- Override the transmission rate, dropping it to 0.03 from April 15 through June 1 (this should drive the
  effective R₀ below 1, suppressing the epidemic two weeks into the projection window).
- 200 posterior samples.

STEP 4: Compare scenarios

Compare the projection results: baseline vs intervention. Report the comparison results and the deltas.

STEP 5: Visualize

Produce a single figure with two panels:
- Left panel: epidemic curves. Plot the median projected Infected_total for both scenarios (Baseline as
  blue, Intervention as orange) with 90% CIs as shaded bands. Mark the end of the calibration period
  (March 31) with a vertical dashed line. Mark the beginning and end of the intervention. If the observed
  data from Step 1 is available, overlay it as black dots for the calibration window.
- Right panel: cumulative attack rate. Plot the median cumulative Recovered_total (as a fraction of
  population) for both scenarios over the full projection period, with 90% CIs.

DELIVERABLES
- The synthetic observed.csv and a validated calibration output.
- Two projection outputs, each inspectable after the fact.
- The comparison of metrics (deltas) between the two scenarios.
- A two-panel figure comparing the scenarios.
- A brief summary: how much does the intervention reduce the peak? Delay it? Lower the final attack rate?
  Are the 90% CIs well-separated or do the scenarios overlap substantially?
