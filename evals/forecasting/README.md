# M6-R Forecasting Synthetic Eval

This suite is virtual evidence only. It uses deterministic in-memory histories and an
independent future ground truth. The ground truth never enters production demand facts,
forecast runs, or inventory plans. Run it with:

```text
python evals/forecasting/run.py
```

The suite covers steady demand, trends, weekly seasonality, intermittent demand, zero
demand, missing-date evidence, and cold start. It is not a claim of accuracy on a real
platform export; real-data acceptance remains a separate gate.
