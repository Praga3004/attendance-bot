from api.discord import app

# Optional health check route for testing root access
@app.get("/")
def root():
    return {"status": "ok", "message": "Discord Attendance → Google Sheets is running."}