# Bug: CSV parser crashes when a row contains trailing empty columns

Reproduction:

```python
parse_csv("a,b,c\n1,2,\n")
```

Expected:

Should return `["1", "2", None]` for the second row.

Actual:

`IndexError: list index out of range`.
