# elements_verify_118.py

from __future__ import print_function
import pandas as pd

from tdda.constraints.pd.pdconstraints import verify_df

df = pd.read_csv('testdata/elements118.csv')
print(verify_df(df, 'elements92.tdda'))
