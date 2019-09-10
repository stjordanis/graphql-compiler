# Copyright 2019-present Kensho Technologies, LLC.
from collections import namedtuple
from copy import copy

from graphql import print_ast
from graphql.language.ast import (
    Argument, Directive, Document, Field, InlineFragment, ListValue, Name, OperationDefinition,
    SelectionSet, StringValue
)

from ..ast_manipulation import get_only_query_definition
from ..exceptions import GraphQLValidationError
from ..schema import FilterDirective, OutputDirective
from .utils import get_query_runtime_arguments


SubQueryPlan = namedtuple(
    'SubQueryPlan', (
        'plan_id',  # int, unique identifier for this sub-plan
        'query_ast',  # Document, representing a piece of the overall query with directives added
        'schema_id',  # str, identifying the schema that this query piece targets
        'parent_query_plan',  # SubQueryPlan, the query that the current query depends on
        'child_query_plans',  # List[SubQueryPlan], the queries that depend on the current query
    )
)


OutputJoinDescriptor = namedtuple(
    'OutputJoinDescriptor', (
        'output_names',  # Tuple[str, str], (parent output name, child output name)
        'child_query_plan',  # SubQueryPlan, the sub-plan node for which the join happens
                             # between it and its parent sub-plan

        # May be expanded to have more attributes, e.g. is_optional, describing how the join
        # should be made
    )
)


QueryPlanDescriptor = namedtuple(
    'QueryPlanDescriptor', (
        'root_sub_query_plan',  # SubQueryPlan
        'intermediate_output_names',  # frozenset[str], names of outputs to be removed at the end
        'output_join_descriptors',
        # List[OutputJoinDescriptor], describing which outputs should be joined and how
    )
)


def make_query_plan(root_sub_query_node, intermediate_output_names):
    """Return a QueryPlanDescriptor, whose query ASTs have @filters added.

    For each parent of parent and child SubQueryNodes, a new @filter directive will be added
    in the child AST. It will be added on the field whose @output directive has the out_name
    equal to the child's out name as specified in the QueryConnection. The newly added @filter
    will be a 'in_collection' type filter, and the name of the local variable is guaranteed to
    be the same as the out_name of the @output on the parent.

    ASTs contained in the input node and its children nodes will not be modified.

    Args:
        root_sub_query_node: SubQueryNode, representing the base of a query split into pieces
                             that we want to turn into a query plan
        intermediate_output_names: frozenset[str], names of outputs to be removed at the end

    Returns:
        QueryPlanDescriptor namedtuple, containing a tree of SubQueryPlans that wrap
        around each individual query AST, the set of intermediate output names that are
        to be removed at the end, and information on which outputs are to be connect to which
        in what manner
    """
    output_join_descriptors = []

    root_sub_query_plan = SubQueryPlan(
        plan_id=0,
        query_ast=root_sub_query_node.query_ast,
        schema_id=root_sub_query_node.schema_id,
        parent_query_plan=None,
        child_query_plans=[],
    )

    _make_query_plan_recursive(root_sub_query_node, root_sub_query_plan, output_join_descriptors, 1)

    return QueryPlanDescriptor(
        root_sub_query_plan=root_sub_query_plan,
        intermediate_output_names=intermediate_output_names,
        output_join_descriptors=output_join_descriptors,
    )


def _make_query_plan_recursive(sub_query_node, sub_query_plan, output_join_descriptors,
                               next_plan_id):
    """Recursively copy the structure of sub_query_node onto sub_query_plan.

    For each child connection contained in sub_query_node, create a new SubQueryPlan for
    the corresponding child SubQueryNode, add appropriate @filter directive to the child AST,
    and attach the new SubQueryPlan to the list of children of the input sub-query plan.

    Args:
        sub_query_node: SubQueryNode, whose descendents are copied over onto sub_query_plan.
                        It is not modified by this function
        sub_query_plan: SubQueryPlan, whose list of child query plans and query AST are
                        modified
        output_join_descriptors: List[OutputJoinDescriptor], describing which outputs should be
                                 joined and how
        next_plan_id: int, the next available plan ID to use. IDs at and above this number are free.
    """
    # Iterate through child connections of query node
    for child_query_connection in sub_query_node.child_query_connections:
        child_sub_query_node = child_query_connection.sink_query_node
        parent_out_name = child_query_connection.source_field_out_name
        child_out_name = child_query_connection.sink_field_out_name

        child_query_type = get_only_query_definition(
            child_sub_query_node.query_ast, GraphQLValidationError
        )
        child_query_type_with_filter = _add_filter_at_field_with_output(
            child_query_type, child_out_name, parent_out_name
            # @filter's local variable is named the same as the out_name of the parent's @output
        )
        if child_query_type is child_query_type_with_filter:
            raise AssertionError(
                u'An @output directive with out_name "{}" is unexpectedly not found in the '
                u'AST "{}".'.format(child_out_name, child_query_type)
            )
        else:
            new_child_query_ast = Document(
                definitions=[child_query_type_with_filter]
            )

        # Create new SubQueryPlan for child
        child_sub_query_plan = SubQueryPlan(
            plan_id=next_plan_id,
            query_ast=new_child_query_ast,
            schema_id=child_sub_query_node.schema_id,
            parent_query_plan=sub_query_plan,
            child_query_plans=[],
        )
        next_plan_id += 1

        # Add new SubQueryPlan to parent's child list
        sub_query_plan.child_query_plans.append(child_sub_query_plan)

        # Add information about this edge
        new_output_join_descriptor = OutputJoinDescriptor(
            output_names=(parent_out_name, child_out_name),
            child_query_plan=child_sub_query_plan,
        )
        output_join_descriptors.append(new_output_join_descriptor)

        # Recursively repeat on child SubQueryPlans
        _make_query_plan_recursive(
            child_sub_query_node, child_sub_query_plan, output_join_descriptors, next_plan_id
        )


def _add_filter_at_field_with_output(ast, field_out_name, input_filter_name):
    """Return an AST with @filter added at the field with the specified @output, if found.

    Args:
        ast: Field, InlineFragment, or OperationDefinition, an AST Node type that occurs in
             the selections of a SelectionSet. It is not modified by this function
        field_out_name: str, the out_name of an @output directive. This function will create
                        a new @filter directive on the field that has an @output directive
                        with this out_name
        input_filter_name: str, the name of the local variable in the new @filter directive
                           created

    Returns:
        Field, InlineFragment, or OperationDefinition, identical to the input ast except
        with an @filter added at the specified field if such a field is found. If no changes
        were made, this is the same object as the input
    """
    if not isinstance(ast, (Field, InlineFragment, OperationDefinition)):
        raise AssertionError(
            u'Input AST is of type "{}", which should not be a selection.'
            u''.format(type(ast).__name__)
        )

    if isinstance(ast, Field):
        # Check whether this field has the expected directive, if so, modify and return
        if (
            ast.directives is not None and
            any(
                _is_output_directive_with_name(directive, field_out_name)
                for directive in ast.directives
            )
        ):
            new_directives = copy(ast.directives)
            new_directives.append(_get_in_collection_filter_directive(input_filter_name))
            new_ast = copy(ast)
            new_ast.directives = new_directives
            return new_ast

    if ast.selection_set is None:  # Nothing to recurse on
        return ast

    # Otherwise, recurse and look for field with desired out_name
    made_changes = False
    new_selections = []
    for selection in ast.selection_set.selections:
        new_selection = _add_filter_at_field_with_output(
            selection, field_out_name, input_filter_name
        )
        if new_selection is not selection:  # Changes made somewhere down the line
            if not made_changes:
                made_changes = True
            else:
                # Change has already been made, but there is a new change. Implies that multiple
                # fields have the @output directive with the desired name
                raise GraphQLValidationError(
                    u'There are multiple @output directives with the out_name "{}"'.format(
                        field_out_name
                    )
                )
        new_selections.append(new_selection)

    if made_changes:
        new_ast = copy(ast)
        new_ast.selection_set = SelectionSet(selections=new_selections)
        return new_ast
    else:
        return ast


def _is_output_directive_with_name(directive, out_name):
    """Return whether or not the input is an @output directive with the desired out_name."""
    if not isinstance(directive, Directive):
        raise AssertionError(u'Input "{}" is not a directive.'.format(directive))
    return (
        directive.name.value == OutputDirective.name and
        directive.arguments[0].value.value == out_name
    )


def _get_in_collection_filter_directive(input_filter_name):
    """Create a @filter directive with in_collecion operation and the desired variable name."""
    return Directive(
        name=Name(value=FilterDirective.name),
        arguments=[
            Argument(
                name=Name(value='op_name'),
                value=StringValue(value='in_collection'),
            ),
            Argument(
                name=Name(value='value'),
                value=ListValue(
                    values=[
                        StringValue(value=u'$' + input_filter_name),
                    ],
                ),
            ),
        ],
    )


def print_query_plan(query_plan_descriptor, indentation_depth=4):
    """Return a string describing query plan."""
    query_plan_strings = [u'']
    plan_and_depth = _get_plan_and_depth_in_dfs_order(query_plan_descriptor.root_sub_query_plan)

    for query_plan, depth in plan_and_depth:
        line_separation = u'\n' + u' ' * indentation_depth * depth
        query_plan_strings.append(line_separation)

        query_str = u'Execute subplan ID {} in schema named "{}":\n'.format(
            query_plan.plan_id, query_plan.schema_id)
        query_str += print_ast(query_plan.query_ast)
        query_str = query_str.replace(u'\n', line_separation)
        query_plan_strings.append(query_str)

    query_plan_strings.append(u'\n\nJoin together outputs as follows: ')
    query_plan_strings.append(str([
        ' '.join([
            str(descriptor.output_names),
            'between subplan IDs',
            str([
                descriptor.child_query_plan.parent_query_plan.plan_id,
                descriptor.child_query_plan.plan_id
            ])])
        for descriptor in query_plan_descriptor.output_join_descriptors
    ]))
    query_plan_strings.append(u'\n\nRemove the following outputs at the end: ')
    query_plan_strings.append(str(set(query_plan_descriptor.intermediate_output_names)) + u'\n')

    return ''.join(query_plan_strings)


def _get_plan_and_depth_in_dfs_order(query_plan):
    """Return a list of topologically sorted (query plan, depth) tuples."""
    def _get_plan_and_depth_in_dfs_order_helper(query_plan, depth):
        plan_and_depth_in_dfs_order = [(query_plan, depth)]
        for child_query_plan in query_plan.child_query_plans:
            plan_and_depth_in_dfs_order.extend(
                _get_plan_and_depth_in_dfs_order_helper(child_query_plan, depth + 1)
            )
        return plan_and_depth_in_dfs_order
    return _get_plan_and_depth_in_dfs_order_helper(query_plan, 0)


def execute_query_plan(schema_id_to_execution_func, query_plan_descriptor, query_args):
    """Execute the given query plan and return the produced results."""
    result_components_by_plan_id = {}

    stitching_output_names_by_parent_plan_id = dict()
    for join_descriptor in query_plan_descriptor.output_join_descriptors:
        parent_plan_id = join_descriptor.child_query_plan.parent_query_plan.plan_id
        stitching_output_names_by_parent_plan_id.setdefault(parent_plan_id, []).append(
            join_descriptor.output_names)

    full_query_args = dict(query_args)

    plan_and_depth = _get_plan_and_depth_in_dfs_order(query_plan_descriptor.root_sub_query_plan)

    for query_plan, _ in plan_and_depth:
        plan_id = query_plan.plan_id
        schema_id = query_plan.schema_id

        subquery_graphql = print_ast(query_plan.query_ast)

        print('\n\n********* BEGIN *********\n')
        print(subquery_graphql)

        # HACK(predrag): Add proper error checking for missing arguments here.
        # HACK(predrag): Don't bother running queries if the previous query's stitching outputs
        #                returned no values to pass to the next query.
        subquery_args = {
            argument_name: full_query_args[argument_name]
            for argument_name in get_query_runtime_arguments(query_plan.query_ast)
        }

        print(subquery_args)

        # Run the query and save the results.
        execution_func = schema_id_to_execution_func[schema_id]
        subquery_result = execution_func(subquery_graphql, subquery_args)
        result_components_by_plan_id[plan_id] = subquery_result

        print(subquery_result)

        # Capture and record any values that will be used for stitching by other subqueries.
        child_extra_output_names = {
            # The .get() call is to handle the case of query plans with no children.
            # They have no extra output values for their children, on account of having no children.
            output_name
            for output_name, _ in stitching_output_names_by_parent_plan_id.get(plan_id, [])
        }
        child_extra_output_values = {
            # Make sure we deduplicate the values -- there's no point in running subqueries
            # with duplicated runtime argument values.
            output_name: set()
            for output_name in child_extra_output_names
        }
        for subquery_row in subquery_result:
            for output_name in child_extra_output_names:
                # We intentionally discard None values -- None is never a foreign key value.
                # This is standard in all relational systems as well.
                output_value = subquery_row.get(output_name, None)
                if output_value is not None:
                    child_extra_output_values[output_name].add(output_value)
        # TODO(predrag): Use the "merge_disjoint_dicts" function here,
        #                there should never be any overlap here.
        new_query_args = {
            # Argument values cannot be sets, so we turn the sets back into lists.
            output_argument_name: list(child_extra_output_values[output_argument_name])
            for output_argument_name in child_extra_output_names
        }
        full_query_args.update(new_query_args)

        print(new_query_args)
        print('\n********** END ***********\n')

    join_indexes_by_plan_id = _make_join_indexes(
        query_plan_descriptor, result_components_by_plan_id)

    joined_results = _join_results(
        result_components_by_plan_id, join_indexes_by_plan_id,
        result_components_by_plan_id[query_plan_descriptor.root_sub_query_plan.plan_id],
        query_plan_descriptor.output_join_descriptors)

    return _drop_intermediate_outputs(
        query_plan_descriptor.intermediate_output_names, joined_results)


def _make_join_indexes(query_plan_descriptor, result_components_by_plan_id):
    """Return a dict from child plan id to a join index between its and its parents' rows."""
    join_indexes_by_plan_id = dict()

    for join_descriptor in query_plan_descriptor.output_join_descriptors:
        child_plan_id = join_descriptor.child_query_plan.plan_id
        _, child_output_name = join_descriptor.output_names

        if child_plan_id in join_indexes_by_plan_id:
            raise AssertionError('Unreachable code reached: {} {} {}'
                                 .format(child_plan_id, join_indexes_by_plan_id,
                                         query_plan_descriptor.output_join_descriptors))

        join_indexes_by_plan_id[child_plan_id] = _make_join_index_for_output(
            result_components_by_plan_id[child_plan_id], child_output_name)

    return join_indexes_by_plan_id


def _make_join_index_for_output(results, join_output_name):
    """Return a dict of each value of the join column to a list of row indexes where it appears."""
    print('making join index on column ', join_output_name)
    print(results)

    join_index = {}
    for row_index, row in enumerate(results):
        join_value = row[join_output_name]
        join_index.setdefault(join_value, []).append(row_index)

    return join_index


def _join_results(result_components_by_plan_id, join_indexes_by_plan_id,
                  current_results, join_descriptors):
    """Return the merged results across all subplans using the calculated join indexes."""
    if len(join_descriptors) == 0:
        # No further joining to be done!
        return current_results

    next_results = []

    next_join_descriptor = join_descriptors[0]
    remaining_join_descriptors = join_descriptors[1:]

    join_plan_id = next_join_descriptor.child_query_plan.plan_id
    join_index = join_indexes_by_plan_id[join_plan_id]
    joining_results = result_components_by_plan_id[join_plan_id]
    join_from_key, join_to_key = next_join_descriptor.output_names

    for current_row in current_results:
        join_value = current_row[join_from_key]

        # To get inner join semantics, we don't output results that don't have matches.
        # When we add support for stitching across @optional edges, we'll need to update this
        # code to also output results even when the join index doesn't contain matches.
        for join_matched_index in join_index.get(join_value, []):
            joining_row = joining_results[join_matched_index]
            next_results.append(dict(current_row, **joining_row))

    return _join_results(result_components_by_plan_id, join_indexes_by_plan_id,
                         next_results, remaining_join_descriptors)


def _drop_intermediate_outputs(columns_to_drop, results):
    """Return the provided results with the specified column names dropped."""
    processed_results = []

    for row in results:
        processed_results.append({
            key: value
            for key, value in row.items()
            if key not in columns_to_drop
        })

    return processed_results