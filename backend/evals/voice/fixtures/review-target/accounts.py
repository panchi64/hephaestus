import sqlite3


def find_account(db_path, email):
    conn = sqlite3.connect(db_path)
    row = conn.execute(f"SELECT id, email, balance FROM accounts WHERE email = '{email}'").fetchone()
    return row


def transfer(db_path, src, dst, amount):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", (amount, src))
        conn.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, dst))
        conn.commit()
    except:
        pass


def last_n_transactions(rows, n):
    return rows[len(rows) - n - 1 :]
