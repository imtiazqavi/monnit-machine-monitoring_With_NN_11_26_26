# Monnit Machine Monitoring Platform v3 — Neural + Logistic + Rules

## Model methods
- Logistic Regression: external coefficients entered on Setup Page 1
- Rule Based: conditions configured on Setup Page 1
- Neural Network: train offline with `offline_nn_trainer.py`, upload exported JSON, map Y outputs to T states, and test manual X inputs online

The neural model estimates **seconds in each state during the preceding inference interval** (normally 600 seconds). It does not force a single state for the whole interval.

## Other additions
- Admin user from Render secrets can add/remove standard users
- Historical daily table records `last_setting_user`
- Machine Group Number + Machine Group Name
- Sensor Battery Health page from Monnit `batteryLevel`
- Colored state charts and legends for today and 30-day state history

## GitHub structure
```
render.yaml
requirements.txt
requirements_offline_nn.txt
offline_nn_trainer.py
shared/
  auth.py db.py engine.py metrics.py schema.sql
webapp/
  app.py
  templates/
worker/
  worker.py
cron/
  archive_daily.py
```

## Render deployment
1. Upload this directory to a private GitHub repository **with folders preserved**.
2. Render Dashboard → New → Blueprint → select repo.
3. Keep the existing Blueprint resources.
4. Enter `PORTAL_USERNAME`, `PORTAL_PASSWORD`, and `ERP_API_KEY` when prompted.
5. Deploy. The schema auto-migrates using `CREATE/ALTER ... IF NOT EXISTS`.
6. iMonnit HTTP webhook URL: `https://YOUR-SERVICE.onrender.com/monnit`.

## Offline neural training
Install locally:
```
pip install -r requirements_offline_nn.txt
```
Run:
```
python offline_nn_trainer.py
```
Excel training headers must be `X1, X2, ...` and `Y1, Y2, ...`.
