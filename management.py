import os
from datetime import datetime, timezone

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

db = SQLAlchemy()
_management_app = Flask("cryptobot-management-db")


def utcnow():
    return datetime.now(timezone.utc)


class ManagementUser(db.Model):
    __tablename__ = "management_users"
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(64), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(160))
    bot_username = db.Column(db.String(160), default="achilles_trojanbot", nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    groups = db.relationship("ManagementGroup", backref="user", cascade="all, delete-orphan")
    signals = db.relationship("ManagementSignal", backref="user", cascade="all, delete-orphan")


class ManagementGroup(db.Model):
    __tablename__ = "management_groups"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("management_users.id"), nullable=False, index=True)
    chat_id = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    monitored = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "chat_id", name="uq_management_user_chat"),)


class ManagementSignal(db.Model):
    __tablename__ = "management_signals"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("management_users.id"), nullable=False, index=True)
    token = db.Column(db.String(160), nullable=False, index=True)
    contract_address = db.Column(db.String(255))
    link = db.Column(db.Text)
    source_chat_id = db.Column(db.String(128))
    source_chat_name = db.Column(db.String(255))
    raw_message = db.Column(db.Text)
    captured_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class ManagementBotConfig(db.Model):
    __tablename__ = "management_bot_configs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("management_users.id"), nullable=False, index=True)
    bot_username = db.Column(db.String(160), nullable=False)
    label = db.Column(db.String(160))
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "bot_username", name="uq_management_user_bot"),)


def init_management():
    database_url = os.getenv("DATABASE_URL", "sqlite:////tmp/cryptobot_management.db")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    _management_app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    _management_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(_management_app)

    with _management_app.app_context():
        db.create_all()


def available_bots():
    raw = os.getenv("MANAGEMENT_AVAILABLE_BOTS", "achilles_trojanbot")
    return [item.strip().lstrip("@") for item in raw.split(",") if item.strip()]


def _user(phone, create=True, bot_username="achilles_trojanbot"):
    with _management_app.app_context():
        user = ManagementUser.query.filter_by(phone=phone).first()
        if not user and create:
            user = ManagementUser(phone=phone, bot_username=bot_username)
            db.session.add(user)
            db.session.flush()
        if user:
            user.last_seen = utcnow()
            if bot_username:
                user.bot_username = bot_username
            db.session.commit()
        return user.id if user else None


def sync_user(phone, bot_username="achilles_trojanbot"):
    return _user(phone, True, bot_username)


def sync_groups(phone, chat_ids):
    with _management_app.app_context():
        user = ManagementUser.query.filter_by(phone=phone).first()
        if not user:
            user = ManagementUser(phone=phone)
            db.session.add(user)
            db.session.flush()

        selected = {str(chat_id) for chat_id in chat_ids}
        for chat_id in selected:
            group = ManagementGroup.query.filter_by(user_id=user.id, chat_id=chat_id).first()
            if not group:
                group = ManagementGroup(user_id=user.id, chat_id=chat_id, name=chat_id, monitored=True)
                db.session.add(group)
            else:
                group.monitored = True
                group.updated_at = utcnow()

        for group in ManagementGroup.query.filter_by(user_id=user.id).all():
            if group.chat_id not in selected:
                group.monitored = False
                group.updated_at = utcnow()

        user.last_seen = utcnow()
        db.session.commit()


def sync_available_groups(phone, groups):
    """Upsert Telegram dialog names without changing monitoring state."""
    with _management_app.app_context():
        user = ManagementUser.query.filter_by(phone=phone).first()
        if not user:
            user = ManagementUser(phone=phone)
            db.session.add(user)
            db.session.flush()

        for item in groups:
            chat_id = str(item.get("id"))
            if not chat_id:
                continue
            group = ManagementGroup.query.filter_by(user_id=user.id, chat_id=chat_id).first()
            if not group:
                group = ManagementGroup(
                    user_id=user.id,
                    chat_id=chat_id,
                    name=item.get("name") or chat_id,
                    monitored=False,
                )
                db.session.add(group)
            else:
                group.name = item.get("name") or group.name or chat_id

        user.last_seen = utcnow()
        db.session.commit()


def set_group_monitoring(phone, chat_id, monitored):
    with _management_app.app_context():
        user = ManagementUser.query.filter_by(phone=phone).first()
        if not user:
            return False

        group = ManagementGroup.query.filter_by(user_id=user.id, chat_id=str(chat_id)).first()
        if not group:
            group = ManagementGroup(
                user_id=user.id,
                chat_id=str(chat_id),
                name=str(chat_id),
                monitored=bool(monitored),
            )
            db.session.add(group)
        else:
            group.monitored = bool(monitored)
            group.updated_at = utcnow()

        user.last_seen = utcnow()
        db.session.commit()
        return True


def remove_group(phone, chat_id):
    with _management_app.app_context():
        user = ManagementUser.query.filter_by(phone=phone).first()
        if not user:
            return False
        group = ManagementGroup.query.filter_by(user_id=user.id, chat_id=str(chat_id)).first()
        if not group:
            return False
        db.session.delete(group)
        db.session.commit()
        return True


def set_user_bot(phone, bot_username):
    bot_username = (bot_username or "").strip().lstrip("@")
    if not bot_username or bot_username not in available_bots():
        return False

    with _management_app.app_context():
        user = ManagementUser.query.filter_by(phone=phone).first()
        if not user:
            user = ManagementUser(phone=phone, bot_username=bot_username)
            db.session.add(user)
            db.session.flush()
        user.bot_username = bot_username

        config = ManagementBotConfig.query.filter_by(
            user_id=user.id, bot_username=bot_username
        ).first()
        if not config:
            config = ManagementBotConfig(
                user_id=user.id,
                bot_username=bot_username,
                label=bot_username,
                enabled=True,
            )
            db.session.add(config)
        else:
            config.enabled = True
            config.updated_at = utcnow()

        user.last_seen = utcnow()
        db.session.commit()
        return True


def record_signal(phone, signal, source_chat_id=None, source_chat_name=None, raw_message=None):
    with _management_app.app_context():
        user = ManagementUser.query.filter_by(phone=phone).first()
        if not user:
            user = ManagementUser(phone=phone)
            db.session.add(user)
            db.session.flush()

        item = ManagementSignal(
            user_id=user.id,
            token=signal.get("token", "UNKNOWN"),
            contract_address=signal.get("contract_address"),
            link=signal.get("link"),
            source_chat_id=source_chat_id,
            source_chat_name=source_chat_name,
            raw_message=raw_message,
        )
        db.session.add(item)
        user.last_seen = utcnow()
        db.session.commit()
        return item.id


def _group_payload(groups):
    return [
        {
            "id": g.id,
            "chat_id": g.chat_id,
            "name": g.name,
            "monitored": g.monitored,
        }
        for g in groups
    ]


def _signal_payload(signals):
    return [
        {
            "id": s.id,
            "token": s.token,
            "contract_address": s.contract_address,
            "link": s.link,
            "source_chat_id": s.source_chat_id,
            "source_chat_name": s.source_chat_name,
            "raw_message": s.raw_message,
            "captured_at": s.captured_at.isoformat(),
        }
        for s in signals
    ]


def get_management_snapshot(phone):
    with _management_app.app_context():
        user = ManagementUser.query.filter_by(phone=phone).first()
        if not user:
            return None

        groups = ManagementGroup.query.filter_by(user_id=user.id).order_by(
            ManagementGroup.name.asc()
        ).all()
        signals = ManagementSignal.query.filter_by(user_id=user.id).order_by(
            ManagementSignal.captured_at.desc()
        ).limit(100).all()
        bots = ManagementBotConfig.query.filter_by(user_id=user.id).order_by(
            ManagementBotConfig.label.asc()
        ).all()

        return {
            "user": {
                "id": user.id,
                "phone": user.phone,
                "display_name": user.display_name,
                "bot_username": user.bot_username,
                "active": user.active,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_seen": user.last_seen.isoformat() if user.last_seen else None,
            },
            "groups": _group_payload(groups),
            "signals": _signal_payload(signals),
            "bots": [
                {
                    "id": b.id,
                    "bot_username": b.bot_username,
                    "label": b.label or b.bot_username,
                    "enabled": b.enabled,
                    "selected": b.bot_username == user.bot_username,
                }
                for b in bots
            ],
            "available_bots": available_bots(),
        }


def get_management_overview():
    with _management_app.app_context():
        users = ManagementUser.query.order_by(ManagementUser.created_at.desc()).all()
        total_groups = ManagementGroup.query.count()
        monitored_groups = ManagementGroup.query.filter_by(monitored=True).count()
        total_signals = ManagementSignal.query.count()

        return {
            "stats": {
                "users": len(users),
                "active_users": sum(1 for u in users if u.active),
                "groups": total_groups,
                "monitored_groups": monitored_groups,
                "signals": total_signals,
            },
            "users": [
                {
                    "id": u.id,
                    "phone": u.phone,
                    "display_name": u.display_name,
                    "bot_username": u.bot_username,
                    "active": u.active,
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                    "last_seen": u.last_seen.isoformat() if u.last_seen else None,
                    "groups": _group_payload(u.groups),
                    "signals": _signal_payload(
                        ManagementSignal.query.filter_by(user_id=u.id)
                        .order_by(ManagementSignal.captured_at.desc())
                        .limit(100).all()
                    ),
                }
                for u in users
            ],
        }
