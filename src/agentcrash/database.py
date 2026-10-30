from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    @staticmethod
    def _enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._ensure_order_intent_key()

    def _ensure_order_intent_key(self) -> None:
        """Inline migration for pre-existing SQLite databases.

        `create_all` does not add columns to existing tables, so databases
        created before the intent-key column existed get it via ALTER TABLE.
        New databases already have the column; the PRAGMA check makes this
        a no-op for them.
        """
        from sqlalchemy import inspect as sqlalchemy_inspect
        from sqlalchemy import text

        columns = [column["name"] for column in sqlalchemy_inspect(self.engine).get_columns("orders")]
        if "intent_key" not in columns:
            with self.engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN intent_key VARCHAR(100)"))

    def sessions(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield session

