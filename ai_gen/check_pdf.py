import sys

try:
    import PyPDF2
    print("PyPDF2 is installed")
except ImportError:
    print("PyPDF2 not found")

try:
    import pdfplumber
    print("pdfplumber is installed")
except ImportError:
    print("pdfplumber not found")

try:
    import fitz
    print("fitz (PyMuPDF) is installed")
except ImportError:
    print("fitz not found")
