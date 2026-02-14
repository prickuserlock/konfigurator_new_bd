from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from connection import conn, cur
from schema import init_db

from app_web.routes import register_routes
from app_bot.manager import start_all_bots

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# init DB schema (PostgreSQL)
init_db(conn, cur)

# web routes
register_routes(app)

# autostart bots
@app.on_event("startup")
async def on_startup():
    await start_all_bots()
