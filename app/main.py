import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.api.router import router
from app.api.challenges import router as challenges_router
from app.core.config import PORT
from app.core.database import Base, engine, migrate

Base.metadata.create_all(bind=engine)
migrate(engine)  # 旧存档补齐申诉/复核链路列（幂等）

app = FastAPI(title="引力跳板：星际弹弓轨道规划游戏")
app.include_router(router)
app.include_router(challenges_router)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
STATIC_DIR = os.path.abspath(STATIC_DIR)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
