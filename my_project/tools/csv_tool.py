import pandas as pd
from langchain_core.tools import tool

@tool
def read_csv_data(file_path: str) -> str:
    """Reads a CSV file and returns its contents as a JSON string."""
    try:
        df = pd.read_csv(file_path)
        df.dropna(how='all', inplace=True)
        return df.to_json(orient="records")
    except Exception as e:
        return f"Tool Error reading CSV: {str(e)}"