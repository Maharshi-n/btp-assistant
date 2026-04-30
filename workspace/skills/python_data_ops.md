# Python Data Operations

Use `run_python` whenever the task involves:
- Merging or joining two or more files (Excel, CSV)
- Filtering rows by condition
- Aggregating/grouping data (sum, count, average by category)
- Deduplicating records
- Matching records between a database query result and a local file
- Any transformation that would be lossy or error-prone if done by the LLM directly

## Rules

1. **Never transform tabular data by reading it into the LLM context.** Use `run_python` instead.
2. Always `print()` the row count and output file path at the end of the script so the observation loop can verify success.
3. If `run_python` returns `Exit: 1`, read the `Stderr:` section, fix the code, and call `run_python` again.
4. File paths in scripts should be relative to the project root: `workspace/filename.xlsx`

## Standard Imports

```python
import pandas as pd
import pathlib
```

## Patterns

### Read Excel / CSV
```python
df = pd.read_excel('workspace/file.xlsx')      # Excel
df = pd.read_csv('workspace/file.csv')         # CSV
```

### Filter rows
```python
result = df[df['status'] == 'active']
```

### Merge two files (left join on a key column)
```python
df1 = pd.read_excel('workspace/sales.xlsx')
df2 = pd.read_excel('workspace/customers.xlsx')
merged = df1.merge(df2, on='customer_id', how='left')
merged.to_excel('workspace/merged_output.xlsx', index=False)
print(f'Done: {len(merged)} rows written to workspace/merged_output.xlsx')
```

### Aggregate / group by
```python
result = df.groupby('region')['revenue'].sum().reset_index()
result.to_excel('workspace/revenue_by_region.xlsx', index=False)
print(f'Done: {len(result)} regions in workspace/revenue_by_region.xlsx')
```

### Match DB query result with local file
```python
# query_database saves result to workspace/tmp/query_result_<ts>.xlsx
# pass that path to run_python
db_df = pd.read_excel('workspace/tmp/query_result_1234567890.xlsx')
local_df = pd.read_excel('workspace/local_users.xlsx')
matched = db_df.merge(local_df, on='email', how='inner')
matched.to_excel('workspace/matched_users.xlsx', index=False)
print(f'Matched: {len(matched)} users in workspace/matched_users.xlsx')
```

### Install a missing package (add at top of script if needed)
```python
import subprocess
subprocess.run(['pip', 'install', 'some-package', '-q'], check=True)
import some_package
```
