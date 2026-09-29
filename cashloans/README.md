# Codnell Cash Loans

Run:
    python -m venv venv && venv\Scripts\activate     (Windows)   |   source venv/bin/activate  (Mac/Linux)
    pip install -r requirements.txt
    python app.py
Open http://127.0.0.1:5000. First sign-in: codnellsmall@gmail.com / ChangeMe123! (you must change it immediately).
Set OWNER_EMAIL / OWNER_PASSWORD env vars before the first run to choose your own.
Product settings (default interest, max amount, max term) are at the top of app.py.
The database lives in instance/loans.db - back that file up.
