from uniborg.util import embed2, brishz
from brish import zs
from pynight.common_ss import ss_get
from pynight.common_files import mkdir
import sqlite3
import os
from telethon import (
    TelegramClient,
    events,
    Button,
)
from sqlalchemy import create_engine, Column, Integer, String, and_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Setting up SQLAlchemy
Base = declarative_base()


class UserFormat(Base):
    __tablename__ = "user_formats"
    user_id = Column(Integer, primary_key=True)
    format = Column(String, nullable=False)


# Create an SQLite database engine and session
db_path = os.path.expanduser("~/.borg/papersonegai/main.db")
mkdir(db_path, do_dirname=True)
engine = create_engine(f"sqlite:///{db_path}", echo=False)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()


def get_user_format(user_id):
    result = session.query(UserFormat).filter_by(user_id=user_id).first()
    return result.format if result else None


def set_user_format(user_id, format):
    user_format = session.query(UserFormat).filter_by(user_id=user_id).first()
    if user_format:
        user_format.format = format
    else:
        user_format = UserFormat(user_id=user_id, format=format)
        session.add(user_format)
    session.commit()


# Ensure to close the session and connection when the script or application ends
import atexit


def close_session():
    session.close()


atexit.register(close_session)


@borg.on(events.NewMessage(pattern=r"/format"))
async def format_command_handler(event):
    keyboard = [
        [Button.inline("JSON", "json_format"), Button.inline("CSV", "csv_format")],
    ]
    await borg.send_message(
        event.sender_id, "Please select a format:", buttons=keyboard
    )


@borg.on(events.CallbackQuery(pattern="json_format"))
async def json_format_handler(event):
    set_user_format(event.sender_id, "json")
    await event.answer("You selected JSON format!")


@borg.on(events.CallbackQuery(pattern="csv_format"))
async def csv_format_handler(event):
    set_user_format(event.sender_id, "csv")
    await event.answer("You selected CSV format!")


@borg.on(events.NewMessage(pattern=r"^(?!/)(.*)$"))
async def _(event):
    if (
        event.message.forward != None
        or event.reply_to_msg_id != None
        or not event.sender
    ):
        return

    reqs = event.pattern_match.group(1)
    for req in reqs.split("\n"):
        first_name = event.sender.first_name
        sender_id = event.sender_id
        print(
            f"User (id={sender_id}, username={event.sender.username}, name={first_name}) requested {req}"
        )

        output_format = get_user_format(sender_id) or "csv"
        mode = "v1"
        adder = first_name or ""
        json_indent = 2
        parallel = True
        flat_p = mode != "all"

        results = ss_get(
            urls=[req],
            adder=adder,
            flat_p=flat_p,
            mode=mode,
            output_format=output_format,
            json_indent=json_indent,
            parallel=parallel,
        )

        for res in results:
            if res.success:
                await event.reply(f"```\n{res.value}\n```", parse_mode="markdown")
            else:
                await event.reply(
                    f"Error:\n```\n{res.error_message}\n```", parse_mode="markdown"
                )
