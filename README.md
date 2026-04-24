# Sports Analytics

Personal performance analytics platform built on Garmin GPX activity data.

## Goal
Parse and analyse thousands of GPX files exported from Garmin to detect performance patterns across time, activity types, and conditions.

## Features (planned)
- GPX batch parser — extract metrics from all activities
- Activity database — structured storage of parsed data
- Performance trend analysis — pace, HR, elevation, cadence over time
- Pattern detection — identify peak performance conditions
- Interactive dashboard — visualise progress and insights

## Data
Place your Garmin GPX exports in the `data/raw/` folder.

## Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

