# Employee Attendance & Analytics API

FastAPI and MongoDB project for the HROne engineering assignment. The API contract and business rules are in `openapi.yaml`; the MongoDB schema is in `DATA_MODEL.md`. Application code is in `app/main.py`.

## Run locally on Windows

1. Install Python 3.11+ and MongoDB 6.0+ (or create a MongoDB Atlas database).
2. From this folder, open PowerShell and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   py -m pip install -r requirements.txt
   ```

3. Create a text file named `.env` in this folder with your own MongoDB settings:

   ```text
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```

   For Atlas, use its connection URI for `MONGO_URI`. Keep credentials private; do not commit `.env`.

4. To load the sample documents into a test database, run `py sample_seed.py`. This deletes existing documents in the two sample collections before loading.
5. Start the API with `py -m uvicorn app.main:app --port 8000 --reload`.
6. Open `http://127.0.0.1:8000/docs` for interactive API documentation.

MongoDB indexes are created by the app at startup. `REVIEW.md` and `DECISIONS.md` describe the starter fixes and implementation choices.
