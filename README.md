# Edge

Personal fantasy sports decision-support dashboard (NFL first, NHL later).

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your keys
```

## Structure

- `pipeline/` — data ingestion (odds, stats, injuries)
- `scoring/` — deterministic scoring engine (Edge / Draft Edge / Wire Edge)
- `config/` — league rules, constants, tunable weights, Supabase client
- `tests/` — unit and integration tests
