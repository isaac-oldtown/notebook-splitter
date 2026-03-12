# Notebook Export

Export Python modules from a Jupyter notebook using simple cell markers.  
Designed to work smoothly with **pre-commit**, so your `src/` code stays automatically synchronized.

This lets you prototype in a notebook while keeping a clean Python package in your repository.

---

## How it works

Add an export directive at the top of a code cell:

```python
# EXPORT: src/utils.py

def my_function():
    return 42
```

If no directive is provided, the cell is exported to:

```
src/main.py
```

Example notebook cells:

```python
# EXPORT: src/math_utils.py

def add(a, b):
    return a + b
```

```python
print(add(2, 3))
```

Generated structure:

```
├── src
│   ├── lib
│   │   ├── __init__.py
│   │   ├── math_utils.py
│   └── main.py
```

The exporter analyzes your code and automatically adds required imports (only the **required** ones).

---

## Installation (pre-commit)

Add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/isaac-oldtown/notebook-export
    rev: v0.2.0
    hooks:
      - id: notebook-export
```

Then install hooks:

```bash
pre-commit install
```

Whenever the notebook changes, the modules are regenerated automatically.

---

## Expected structure

```
project/
├── notebooks/
│   └── main.ipynb
└── .pre-commit-config.yaml
```
