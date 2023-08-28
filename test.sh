#!/bin/bash
source env/bin/activate
export DATABASE_URI="sqlite:////home/exouser/labelq/db/database.db"

cd app

python -m pytest test_main.py