# -*- coding: utf-8 -*-
"""
TDDA constraint discovery and verification for Pandas.

The top-level functions are:

    :py:func:`discover_df`:
        Discover constraints from a Pandas DataFrame.

    :py:func:`verify_df`:
        Verify (check) a Pandas DataFrame, against a set of previously
        discovered constraints.

API
---

"""
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import datetime
import re
import sys

from collections import OrderedDict

import pandas as pd
import numpy as np

from tdda.constraints.base import (
    STANDARD_FIELD_CONSTRAINTS,
    verify,
    native_definite,
    DatasetConstraints,
    FieldConstraints,
    Verification,
    TypeConstraint,
    MinConstraint, MaxConstraint, SignConstraint,
    MinLengthConstraint, MaxLengthConstraint,
    NoDuplicatesConstraint, MaxNullsConstraint,
    AllowedValuesConstraint, RexConstraint,
)
from tdda.constraints.baseconstraints import (
    BaseConstraintVerifier,
    BaseConstraintDiscoverer,
    MAX_CATEGORIES,
)

DEBUG = False

from tdda.rexpy import pdextract

if sys.version_info.major >= 3:
    long = int

# pd.tslib is deprecated in newer versions of Pandas
if hasattr(pd, 'Timestamp'):
    pandas_Timestamp = pd.Timestamp
else:
    pandas_Timestamp = pd.tslib.Timestamp


class PandasConstraintVerifier(BaseConstraintVerifier):
    """
    A :py:class:`PandasConstraintVerifier` object provides methods
    for verifying every type of constraint against a Pandas DataFrame.
    """
    def __init__(self, df, epsilon=None, type_checking=None):
        self.df = df
        BaseConstraintVerifier.__init__(self, epsilon=epsilon,
                                        type_checking=type_checking)

    def is_null(self, value):
        return pd.isnull(value)

    def to_datetime(self, value):
        return pd.to_datetime(value)

    def types_compatible(self, x, y, colname):
        return types_compatible(x, y, colname)

    def allowed_values_exclusions(self):
        # remarkably, Pandas returns various kinds of nulls as
        # unique values, despite not counting them with .nunique()
        return [None, np.nan, pd.NaT]

    def calc_rex_constraint(self, colname, constraint):
        rexes = constraint.value
        if rexes is None:      # a null value is not considered
            return True        # to be an active constraint,
                               # so is always satisfied
        rexes = [re.compile(r) for r in rexes]
        strings = [native_definite(s)
                   for s in self.df[colname].dropna().unique()]

        for s in strings:
            for r in rexes:
                if re.match(r, s):
                    break
            else:
                if DEBUG:
                    print('*** Unmatched string: "%s"' % s)
                return False  # At least one string didn't match
        return True

    def calc_min(self, colname):
        if self.df[colname].dtype == np.dtype('O'):
            return self.df[colname].dropna().min()  # Otherwise -inf!
        else:
            return self.df[colname].min()

    def calc_max(self, colname):
        if self.df[colname].dtype == np.dtype('O'):
            return self.df[colname].dropna().max()
        else:
            return self.df[colname].max()

    def calc_min_length(self, colname):
        return min(len(s) for s in list(self.df[colname].unique())
                          if not pd.isnull(s))

    def calc_max_length(self, colname):
        return max(len(s) for s in list(self.df[colname].unique())
                          if not pd.isnull(s))

    def calc_tdda_type(self, colname):
        return tdda_column_type(self.df[colname])

    def calc_null_count(self, colname):
        return len(self.df) - self.df[colname].count()

    def calc_non_null_count(self, colname):
        return len(self.df) - self.get_null_count(colname)

    def calc_nunique(self, colname):
        return self.df[colname].nunique()

    def calc_unique_values(self, colname):
        values = self.df[colname].unique()
        nullvalues = [v for v in values if pd.isnull(v)]
        nonnullvalues = [v for v in values if not pd.isnull(v)]
        return nullvalues + sorted(nonnullvalues)

    def calc_non_integer_values_count(self, colname):
        values = self.df[colname].dropna()
        non_nulls = self.df[colname].count()
        return non_nulls - (values.astype(int) == values).astype(int).sum()

    def calc_all_non_nulls_boolean(self, colname):
        nn = self.df[colname].dropna()
        return all([type(v) is bool for i, v in nn.iteritems()])

    def repair_field_types(self, constraints):
        # We sometimes haven't inferred the field types correctly for
        # the dataframe (e.g. if we read it from a csv file, "string"
        # fields might look like numeric ones, if they only contain digits).
        # We can try to use the constraint information to try to repair this,
        # but it's not always going to be successful.
        for c in self.df.columns.tolist():
            ser = self.df[c]
            try:
                ctype = constraints[c]['type'].value
                dtype = ser.dtype
                if ctype == 'string' and dtype != pd.np.dtype('O'):
                    is_numeric = True
                    is_real = False
                    for limit in ('min', 'max'):
                        if limit in constraints[c]:
                            limitval = constraints[c][limit].value
                            if type(limitval) in (int, long, float):
                                if type(limitval) == float:
                                    is_real = True
                            else:
                                is_numeric = False
                                break
                    if is_numeric:
                        if is_real:
                            is_real = self.calc_non_integer_values_count(c) > 0
                        self.df.loc[ser.notnull(), c] = ser.astype(str)
                        if not is_real:
                            self.df[c] = self.df[c].str.replace('.0', '')
                elif ctype == 'bool' and dtype == pd.np.dtype('int64'):
                    self.df[c] = ser.astype(bool)
                elif ctype == 'bool' and dtype == pd.np.dtype('int32'):
                    self.df[c] = ser.astype(bool)
            except Exception as e:
                print(e)
                pass


class PandasVerification(Verification):
    """
    A :py:class:`PandasVerification` object adds a :py:meth:`to_frame()`
    method to a :py:class:`tdda.constraints.base.Verification` object.

    This allows the result of constraint verification to be converted to a
    Pandas DataFrame, including columns for the field (column) name,
    the numbers of passes and failures and boolean columns for each
    constraint, with values:

        - ``True``       --- if the constraint was satified for the column
        - ``False``      --- if column failed to satisfy the constraint
        - ``pd.np.NaN``  --- if there was no constraint of this kind
    """
    def __init__(self, *args, **kwargs):
        Verification.__init__(self, *args, **kwargs)

    def to_frame(self):
        """
        Converts object to a Pandas DataFrame.
        """
        return self.verification_to_dataframe(self)

    @staticmethod
    def verification_to_dataframe(ver):
        fields = ver.fields
        df = pd.DataFrame(OrderedDict((
            ('field', list(fields.keys())),
            ('failures', [v.failures for k, v in fields.items()]),
            ('passes', [v.passes for k, v in fields.items()]),
        )))
        kinds_used = set([])
        for field, constraints in fields.items():
            kinds_used = kinds_used.union(set(list(constraints.keys())))
        base_kinds = [k for k in STANDARD_FIELD_CONSTRAINTS if k in kinds_used]
        other_kinds = [k for k in kinds_used if not k in base_kinds]
        for kind in base_kinds + other_kinds:
            df[kind] = [fields[field].get(kind, np.nan) for field in fields]
        return df

    to_dataframe = to_frame


class PandasConstraintDiscoverer(BaseConstraintDiscoverer):
    """
    A :py:class:`PandasConstraintDiscoverer` object is used to discover
    constraints on a Pandas DataFrame.
    """
    def __init__(self, df, inc_rex=False):
        BaseConstraintDiscoverer.__init__(self, inc_rex=inc_rex)
        self.df = df

    def get_column_names(self):
        return list(self.df)

    def discover_field_constraints(self, fieldname):
        min_constraint = max_constraint = None
        min_length_constraint = max_length_constraint = None
        sign_constraint = no_duplicates_constraint = None
        max_nulls_constraint = allowed_values_constraint = None
        rex_constraint = None

        field = self.df[fieldname]
        type_ = tdda_column_type(field)
        if type_ == 'other':
            return None         # Unrecognized or complex
        else:
            type_constraint = TypeConstraint(type_)
        length = len(field)

        if length > 0:  # Things are not very interesting when there is no data
            nNull = int(field.isnull().sum().astype(int))
            nNonNull = int(field.notnull().sum().astype(int))
            assert nNull + nNonNull == length
            if nNull < 2:
                max_nulls_constraint = MaxNullsConstraint(nNull)

            # Useful info:
            uniqs = None
            n_unique = -1   # won't equal number of non-nulls later on
            if type_ in ('string', 'int'):
                n_unique = field.nunique()          # excludes NaN
                if type_ == 'string':
                    if n_unique <= MAX_CATEGORIES:
                        uniqs = list(field.dropna().unique())
                    if uniqs:
                        avc = AllowedValuesConstraint(uniqs)
                        allowed_values_constraint = avc

            if nNonNull > 0:
                if type_ == 'string':
                    # We don't generate a min, max or sign constraints for
                    # strings. But we do generate min and max length
                    # constraints
                    if (uniqs is None       # There were too many for us to
                        and n_unique > 0):  # have bothered getting them all
                        uniqs = list(field.dropna().unique())  # need them now
                    if uniqs:
                        m = min(len(v) for v in uniqs)
                        M = max(len(v) for v in uniqs)
                        min_length_constraint = MinLengthConstraint(m)
                        max_length_constraint = MaxLengthConstraint(M)
                else:
                    # Non-string fields all potentially get min and max values
                    if type_ == 'date':
                        m = field.min()
                        M = field.max()
                        if pd.notnull(m):
                            m = m.to_pydatetime()
                        if pd.notnull(M):
                            M = M.to_pydatetime()
                    else:
                        m = field.min().item()
                        M = field.max().item()
                    if pd.notnull(m):
                        min_constraint = MinConstraint(m)
                    if pd.notnull(M):
                        max_constraint = MaxConstraint(M)

                    # Non-date fields potentially get a sign constraint too.
                    if min_constraint and max_constraint and type_ != 'date':
                        if m == M == 0:
                            sign_constraint = SignConstraint('zero')
                        elif m >= 0:
                            sign = 'positive' if m > 0 else 'non-negative'
                            sign_constraint = SignConstraint(sign)
                        elif M <= 0:
                            sign = 'negative' if M < 0 else 'non-positive'
                            sign_constraint = SignConstraint(sign)
                        # else:
                            # mixed
                    elif pd.isnull(m) and type_ != 'date':
                        sign_constraint = SignConstraint('null')

            if n_unique == nNonNull and n_unique > 1 and type_ != 'real':
                no_duplicates_constraint = NoDuplicatesConstraint()

        if type_ == 'string' and self.inc_rex:
            rex_constraint = RexConstraint(pdextract(field))

        constraints = [c for c in [type_constraint,
                                   min_constraint, max_constraint,
                                   min_length_constraint, max_length_constraint,
                                   sign_constraint, max_nulls_constraint,
                                   no_duplicates_constraint,
                                   allowed_values_constraint,
                                   rex_constraint]
                         if c is not None]
        return FieldConstraints(field.name, constraints)


def tdda_column_type(x):
    """
    Returns the TDDA type of a column.

    Basic TDDA types are one of 'bool', 'int', 'real', 'string' or 'date'.

    If *x* is ``None`` or something Pandas classes as null, 'null' is returned.

    If *x* is not recognized as one of these, 'other' is returned.

    TODO: this implementation knows about Pandas, and it ought not to!
    """
    dt = getattr(x, 'dtype', None)
    if dt == np.dtype('O'):
        return 'string'
    dts = str(dt)
    if 'bool' in dts:
        return 'bool'
    if 'int' in dts:
        return 'int'
    if 'float' in dts:
        return 'real'
    if 'datetime' in dts:
        return 'date'
    if not isinstance(x, pd.core.series.Series) and pd.isnull(x):
        return 'null'
    # Everything else is other, for now, including compound types,
    # unicode in Python2, bytes in Python3 etc.
    return 'other'


def types_compatible(x, y, colname=None):
    """
    Returns boolean indicating whether the coarse_type of *x* and *y* are
    the same, for scalar values.

    If *colname* is provided, and the check fails, a warning is issued
    to stderr.
    """
    ok = coarse_type(x) == coarse_type(y)
    if not ok and colname:
        print('Warning: Failing incompatible types constraint for field %s '
              'of type %s.\n(Constraint value %s of type %s.)'
              % (colname, type(x), y, type(y)), file=sys.stderr)
    return ok


def coarse_type(x):
    """
    Returns the TDDA coarse type of *x*, a scalar value.
    The coarse types combine 'bool', 'int' and 'real' into 'number'.

    Obviously, some people will dislike treating booleans as numbers.
    But it is necessary here.
    """
    t = tdda_type(x)
    return 'number' if t in ('bool', 'int', 'real') else t


def tdda_type(x):
     dt = getattr(x, 'dtype', None)
     if type(x) == str or dt == np.dtype('O'):
         return 'string'
     dts = str(dt)
     if type(x) == bool or 'bool' in dts:
         return 'bool'
     if type(x) in (int, long) or 'int' in dts:
         return 'int'
     if type(x) == float or 'float' in dts:
         return 'real'
     if (type(x) == datetime.datetime or 'datetime' in dts
                 or type(x) == pandas_Timestamp):
         return 'date'
     if x is None or (not isinstance(x, pd.core.series.Series)
                      and pd.isnull(x)):
         return 'null'
     # Everything else is other, for now, including compound types,
     # unicode in Python2, bytes in Python3 etc.
     return 'other'


def verify_df(df, constraints_path, epsilon=None, type_checking=None,
              **kwargs):
    """
    Verify that (i.e. check whether) the Pandas DataFrame provided
    satisfies the constraints in the JSON .tdda file provided.

    Mandatory Inputs:

        *df*:
                            A Pandas DataFrame, to be checked.

        *constraints_path*:
                            The path to a JSON .tdda file (possibly
                            generated by the discover_constraints
                            function, below) containing constraints
                            to be checked.

    Optional Inputs:

        *epsilon*:
                            When checking minimum and maximum values
                            for numeric fields, this provides a
                            tolerance. The tolerance is a proportion
                            of the constraint value by which the
                            constraint can be exceeded without causing
                            a constraint violation to be issued.
                            With the default value of epsilon
                            (:py:const:`EPSILON_DEFAULT` = 0.01, i.e. 1%),
                            values can be up to 1% larger than a max constraint
                            without generating constraint failure,
                            and minimum values can be up to 1% smaller
                            that the minimum constraint value without
                            generating a constraint failure. (These
                            are modified, as appropraite, for negative
                            values.)

                            NOTE: A consequence of the fact that these
                            are proportionate is that min/max values
                            of zero do not have any tolerance, i.e.
                            the wrong sign always generates a failure.

        *type_checking*:
                            'strict' or 'sloppy'.
                            Because Pandas silently, routinely and
                            automatically "promotes" integer and boolean
                            columns to reals and objects respectively
                            if they contain nulls, strict type checking
                            can be problematical in Pandas. For this reason,
                            type_checking defaults to 'sloppy', meaning
                            that type changes that could plausibly be
                            attriuted to Pandas type promotion will not
                            generate constraint values.

                            If this is set to strict, a Pandas "float"
                            column c will only be allowed to satisfy a
                            an "int" type constraint if:

                                `c.dropnulls().astype(int) == c.dropnulls()`

                            Similarly, Object fields will satisfy a
                            'bool' constraint only if:

                                `c.dropnulls().astype(bool) == c.dropnulls()`

        *report*:
                            'all' or 'fields'.
                            This controls the behaviour of the
                            :py:meth:`~PandasVerification.__str__` method on
                            the resulting :py:class:`~PandasVerification`
                            object (but not its content).

                            The default is 'all', which means that
                            all fields are shown, together with the
                            verification status of each constraint
                            for that field.

                            If report is set to 'fields', only fields for
                            which at least one constraint failed are shown.

                            NOTE: The method also accepts two further
                            parameters to control (not yet implemented)
                            behaviour. 'constraints', will be used to
                            indicate that only failing constraints for
                            failing fields should be shown.
                            'one_per_line' will indicate that each constraint
                            failure should be reported on a separate line.

    Returns:

        :py:class:`~PandasVerification` object.

        This object has attributes:

            - *passed*      --- Number of passing constriants
            - *failures*    --- Number of failing constraints

        It also has a :py:meth:`~PandasVerification.to_frame()` method for
        converting the results of the verification to a Pandas DataFrame,
        and a :py:meth:`~PandasVerification.__str__` method to print
        both the detailed and summary results of the verification.

    Example usage::

        import pandas as pd
        from tdda.constraints.pdconstraints import verify_df

        df = pd.DataFrame({'a': [0, 1, 2, 10, pd.np.NaN],
                           'b': ['one', 'one', 'two', 'three', pd.np.NaN]})
        v = verify_df(df, 'example_constraints.tdda')

        print('Passes:', v.passes)
        print('Failures: %d\\n' % v.failures)
        print(str(v))
        print(v.to_frame())

    See *simple_verification.py* in the :ref:`constraint_examples`
    for a slightly fuller example.

    """
    pdv = PandasConstraintVerifier(df, epsilon=epsilon,
                                   type_checking=type_checking)
    constraints = DatasetConstraints(loadpath=constraints_path)
    pdv.repair_field_types(constraints)
    return verify(constraints, pdv.verifiers(),
                  VerificationClass=PandasVerification, **kwargs)


def discover_df(df, inc_rex=False):
    """
    Automatically discover potentially useful constraints that characterize
    the Pandas DataFrame provided.

    Input:

        *df*:
            any Pandas DataFrame.

    Possible return values:

       -  :py:class:`~tdda.constraints.base.DatasetConstraints` object
       -  ``None``    --- (if no constraints were found).

    This function goes through each column in the DataFrame and, where
    appropriate, generates constraints that describe (and are satisified
    by) this dataframe.

    Assuming it generates at least one constraint for at least one field
    it returns a :py:class:`tdda.constraints.base.DatasetConstraints` object.

    This includes a 'fields' attribute, keyed on the column name.

    The returned :py:class:`~tdda.constraints.base.DatasetConstraints` object
    includes a :py:meth:`~tdda.constraints.base.DatasetContraints.to_json`
    method, which converts the constraints into JSON for saving as a tdda
    constraints file. By convention, such JSON files use a '.tdda'
    extension.

    The JSON constraints file can be used to check whether other datasets
    also satisfy the constraints.

    The kinds of constraints (potentially) generated for each field (column)
    are:

        *type*:
                the (coarse, TDDA) type of the field. One of
                'bool', 'int', 'real', 'string' or 'date'.


        *min*:
                for non-string fields, the minimum value in the column.
                Not generated for all-null columns.

        *max*:
                for non-string fields, the maximum value in the column.
                Not generated for all-null columns.

        *min_length*:
                For string fields, the length of the shortest string(s)
                in the field. N.B. In Python3, this is of course,
                a unicode string length; in Python2, it is an encoded
                string length, which may be less meaningful.

        *max_length*:
                For string fields, the length of the longest string(s)
                in the field.  N.B. In Python3, this is of course,
                a unicode string length; in Python2, it is an encoded
                string length, which may be less meaningful.

        *sign*:
                If all the values in a numeric field have consistent sign,
                a sign constraint will be written with a value chosen from:

                    - positive     --- For all values *v* in field: `v > 0`
                    - non-negative --- For all values *v* in field: `v >= 0`
                    - zero         --- For all values *v* in field: `v == 0`
                    - non-positive --- For all values *v* in field: `v <= 0`
                    - negative     --- For all values *v* in field: `v < 0`
                    - null         --- For all values *v* in field: `v is null`

        *max_nulls*:
                The maximum number of nulls allowed in the field.

                    - If the field has no nulls, a constraint
                      will be written with max_nulls set to zero.
                    - If the field has a single null, a constraint will
                      be written with max_nulls set to one.
                    - If the field has more than 1 null, no constraint
                      will be generated.

        *no_duplicates*:
                For string fields (only, for now), if every
                non-null value in the field is different,
                this constraint will be generated (with value ``True``);
                otherwise no constraint will be generated. So this constraint
                indicates that all the **non-null** values in a string
                field are distinct (unique).

        *allowed_values*:
                 For string fields only, if there are
                 :py:const:`MAX_CATEGORIES` or fewer distinct string
                 values in the dataframe, an AllowedValues constraint
                 listing them will be generated.
                 :py:const:`MAX_CATEGORIES` is currently "hard-wired" to 20.

    Example usage::

        import pandas as pd
        from tdda.constraints.pdconstraints import discover_constraints

        df = pd.DataFrame({'a': [1, 2, 3], 'b': ['one', 'two', pd.np.NaN]})
        constraints = discover_constraints(df)
        with open('example_constraints.tdda', 'w') as f:
            f.write(constraints.to_json())

    See *simple_generation.py* in the :ref:`constraint_examples`
    for a slightly fuller example.

    """
    disco = PandasConstraintDiscoverer(df, inc_rex=inc_rex)
    return disco.discover()


def discover_constraints(df, inc_rex=False):
    """
    Wrapper function to expose :py:func:`discover_df` under an older
    legacy name.
    """
    return discover_df(df, inc_rex=inc_rex)

