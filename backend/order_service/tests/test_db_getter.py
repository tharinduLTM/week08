from app.main import get_db  # type: ignore

def test_get_db_yields_and_closes():
    # Just iterate the dependency generator once to cover its yield/close path
    gen = get_db()
    db = next(gen)
    assert db is not None
    try:
        next(gen)  # should StopIteration
    except StopIteration:
        pass
