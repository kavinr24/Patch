from fastapi import FastAPI

app = FastAPI(title="Patch")


@app.get("/health")
def health_check():
    return {"status": "ok"}
