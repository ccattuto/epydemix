---
id: t02_sir_calibration
title: SIR calibration, posterior quality and two-parameter sensitivity
timeout_s: 5400
tags: calibration, abc-smc, figures
source: epydemix_agent_test2.txt
---
STEP 1: Generate synthetic observed data

Run a forward SIR simulation with these "true" parameters to produce synthetic outbreak data:

- Population: 100,000 (default flat population)
- transmission_rate: 0.35
- recovery_rate: 0.1
- Period: Jan 1 – Apr 30, 2024 (120 days)
- Initial conditions: S = 0.999, I = 0.001, R = 0.0
- 1 simulation (or take the median of a few)

Extract the daily Infected_total time-series from this run and save it as a CSV file (observed.csv with
columns `date` and `cases`). This is the "observed data" we will calibrate against.

STEP 2: Calibrate transmission_rate

Set up a calibration that:

- Uses the same SIR model structure and recovery_rate = 0.1 (fixed).
- Defines a uniform prior on transmission_rate over [0.1, 0.8] — deliberately wide, to test whether the
  calibration can recover the true value.
- Uses observed.csv as the observed data.
- Targets Infected_total as the comparison variable.
- Uses RMSE as the distance function.
- Uses the SMC strategy with 300 particles and 8 generations.

Define the base model setup once and reuse it for the calibration, so that the calibration only specifies
what it adds on top. Check the calibration setup is correct, then run it.

STEP 3: Assess calibration quality

- Inspect the posterior: what is the estimated transmission_rate? Report mean, median, std, and 95% CI.
  How close is the posterior median to the true value of 0.35?
- Inspect the calibration fit: get the fit trajectories (quantiles 0.05, 0.5, 0.95 for Infected_total) and
  compare them to the observed data.
- Produce a figure with two panels:
  - Left panel: posterior distribution of transmission_rate as a histogram, with a vertical line at the
    true value (0.35).
  - Right panel: calibration fit — observed data as black dots, median fit as a blue line, 90% CI as a
    shaded band.

STEP 4: Sensitivity — calibrate both parameters

Now set up a second calibration (reusing the same base model setup) that calibrates both
transmission_rate and recovery_rate:

- transmission_rate: uniform prior [0.1, 0.8]
- recovery_rate: uniform prior [0.03, 0.3]

Run this calibration (300 particles, 8 generations). Then:

- Inspect the joint posterior. Report the estimated values and compare to the true values
  (transmission_rate = 0.35, recovery_rate = 0.1).
- Produce a figure showing the joint posterior as a 2D scatter plot (transmission_rate on the x-axis,
  recovery_rate on the y-axis), with the true values marked as a red cross.

DELIVERABLES

- The observed.csv file with the synthetic data.
- Two validated calibration setups (single-parameter and two-parameter), both built on a shared base.
- Two sets of calibration results, with posterior and fit inspection reported.
- Both figures.
- A brief summary: did the calibration recover the true parameters? How tight are the posteriors? Did
  calibrating two parameters simultaneously make the estimates worse?
