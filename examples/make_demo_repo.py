"""Build a small git repo (a 'shop' package with tests) to try CodeAtlas on.

    python examples/make_demo_repo.py ./demo_repo          # clean, committed
    python examples/make_demo_repo.py ./demo_repo --bug    # plus the uncommitted clamp() bug

Then:  codeatlas check --repo ./demo_repo
(set CODEATLAS_ROOTS to a folder containing ./demo_repo, or run from its parent directory)
"""
import subprocess
import sys
from pathlib import Path

GOOD_CLAMP = "    return max(minimum, min(value, maximum))"
BAD_CLAMP = "    return min(minimum, max(value, maximum))"

FILES = {
    "shop/__init__.py": "",
    "shop/pricing.py": f'''def clamp(value: float, minimum: float, maximum: float) -> float:
    """Keep a value inside an inclusive range."""
{GOOD_CLAMP}


def calculate_discount(subtotal: float, discount_rate: float) -> float:
    """Return a discount amount, with the rate constrained to 0..1."""
    safe_rate = clamp(discount_rate, 0.0, 1.0)
    return round(subtotal * safe_rate, 2)


def discounted_total(subtotal: float, discount_rate: float) -> float:
    """Calculate a non-negative total after applying a discount."""
    discount = calculate_discount(subtotal, discount_rate)
    return max(0.0, round(subtotal - discount, 2))
''',
    "shop/checkout.py": '''from .pricing import discounted_total


def checkout_total(subtotal: float, discount_rate: float, shipping: float = 0.0) -> float:
    shipping = max(0.0, shipping)
    return round(discounted_total(subtotal, discount_rate) + shipping, 2)
''',
    "shop/orders.py": '''from .checkout import checkout_total


def create_order(items, discount_rate: float = 0.0, shipping: float = 0.0) -> dict:
    subtotal = sum(price * qty for price, qty in items)
    return {"subtotal": subtotal, "total": checkout_total(subtotal, discount_rate, shipping)}
''',
    "tests/__init__.py": "",
    "tests/test_pricing.py": '''from shop.pricing import calculate_discount, clamp, discounted_total


def test_clamp_keeps_value_inside_range():
    assert clamp(5, 0, 10) == 5
    assert clamp(-3, 0, 10) == 0
    assert clamp(99, 0, 10) == 10


def test_discount_rate_is_clamped():
    assert calculate_discount(100, 0.2) == 20.0
    assert calculate_discount(100, 5) == 100.0


def test_discounted_total():
    assert discounted_total(100, 0.25) == 75.0


def test_zero_discount_is_a_no_op():
    assert discounted_total(50, 0) == 50.0
''',
    "tests/test_checkout.py": '''from shop.checkout import checkout_total


def test_checkout_total_with_discount_and_shipping():
    assert checkout_total(100, 0.2, 10) == 90


def test_negative_shipping_is_treated_as_zero():
    assert checkout_total(100, 0, -5) == 100
''',
    "tests/test_orders.py": '''from shop.orders import create_order


def test_create_order_summary():
    order = create_order([(100.0, 2), (200.0, 2)], discount_rate=0.1, shipping=20)
    assert order["total"] == 560
''',
}


def git(dest, *args):
    return subprocess.run(["git", "-C", str(dest), "-c", "user.name=Demo", "-c", "user.email=demo@example.com",
                           *args], check=True, capture_output=True, text=True)


def build(dest, bug: bool = False) -> Path:
    dest = Path(dest)
    for rel, text in FILES.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    git(dest, "init", "-q")
    git(dest, "symbolic-ref", "HEAD", "refs/heads/main")
    git(dest, "add", ".")
    git(dest, "commit", "-q", "-m", "initial")
    if bug:
        introduce_bug(dest)
    return dest


def introduce_bug(dest) -> None:
    p = Path(dest) / "shop/pricing.py"
    p.write_text(p.read_text(encoding="utf-8").replace(GOOD_CLAMP, BAD_CLAMP), encoding="utf-8", newline="\n")


def fix_bug(dest) -> None:
    p = Path(dest) / "shop/pricing.py"
    p.write_text(p.read_text(encoding="utf-8").replace(BAD_CLAMP, GOOD_CLAMP), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = build(sys.argv[1], bug="--bug" in sys.argv)
    print(f"demo repo ready: {out.resolve()}")
