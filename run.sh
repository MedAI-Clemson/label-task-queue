#!/bin/bash
source env/bin/activate
export DATABASE_URI="sqlite:////home/exouser/labelq/db/database.db"

cd app

uvicorn main:app --reload --host 0.0.0.0 --port 8000
