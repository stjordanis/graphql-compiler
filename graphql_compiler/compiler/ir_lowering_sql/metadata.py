from sqlalchemy import MetaData, bindparam, or_

from graphql_compiler.compiler import blocks
from .constants import SqlBackend, Cardinality


class BasicEdge:
    def __init__(self, source_column, sink_column, table_name=None):
        self.source_col = source_column
        self.sink_col = sink_column
        self.table_name = table_name


class MultiEdge:
    def __init__(self, junction_edge, final_edge):
        if not isinstance(junction_edge, BasicEdge) or not isinstance(final_edge, BasicEdge):
            raise AssertionError('A multi-edge must be comprised of basic edges.')
        self.junction_edge = junction_edge
        self.final_edge = final_edge


class CompilerMetadata:
    """
    Configuration manager for compilation.

    In order to transform GraphQL to SQL, there needs to be
    additional configuration specified surrounding GraphQL type -> SQL table, GraphQL field -> SQL
    table column, and SQL table to SQL table relationships (for JOIN statements).
    """
    def __init__(self, config, dialect, sqlalchemy_metadata: MetaData):
        """Initialize a new metadata manager."""
        self.config = config
        self.sqlalchemy_metadata = sqlalchemy_metadata
        self._db_backend = SqlBackend(dialect)
        self.table_name_to_table = {
            name.lower(): table for name, table in self.sqlalchemy_metadata.tables.items()
        }

    def get_table(self, node):
        return self._get_table_for_schema_name(node.relative_type)

    def _get_table_for_schema_name(self, schema_name):
        """
        Retrieve a SQLAlchemy table based on the supplied schema name.
        :param schema_name: Name of the type in the GraphQL schema
        :return: Table
        """
        if schema_name not in self.config or 'table_name' not in self.config[schema_name]:
            table_name = schema_name
        else:
            table_name = self.config[schema_name]['table_name']
        return self.get_table_by_name(table_name)

    def get_table_by_name(self, table_name):
        table_name = table_name.lower()
        if table_name not in self.table_name_to_table:
            raise AssertionError(
                'No Table found in SQLAlchemy metadata for table name "{}"'.format(table_name)
            )
        return self.table_name_to_table[table_name]

    @property
    def db_backend(self):
        return self._db_backend

    def get_column_name_for_type(self, schema_type, field_name):
        if schema_type not in self.config:
            return field_name
        column_name = field_name
        schema_config = self.config[schema_type]
        if 'column_names' not in schema_config:
            return column_name
        column_map = schema_config['column_names']
        if field_name not in column_map:
            return column_name
        return column_map[field_name]

    def get_on_clause_for_node(self, node):
        edge = self.get_edge(node)
        if isinstance(edge, BasicEdge):
            source_col = edge.source_col
            sink_col = edge.sink_col
            if node.block.direction == 'in':
                source_col, sink_col = sink_col, source_col
            if edge is None:
                return None
            outer_column = self._get_column_from_table(node.parent_node.table, source_col)
            inner_column = self._get_column_from_table(node.table, sink_col)
            return [(node.from_clause, outer_column == inner_column)]
        elif isinstance(edge, MultiEdge):
            traversal_edge = edge.junction_edge
            junction_table = self.get_table_by_name(traversal_edge.table_name).alias()
            source_col = traversal_edge.source_col
            sink_col = traversal_edge.sink_col
            if node.block.direction == 'in':
                source_col, sink_col = sink_col, source_col

            outer_column = self._get_column_from_table(node.parent_node.from_clause, source_col)
            inner_column = self._get_column_from_table(junction_table, sink_col)
            traversal_onclause = outer_column == inner_column
            if node.in_optional:
                node.parent_node.from_clause = node.parent_node.from_clause.outerjoin(
                    junction_table, onclause=traversal_onclause
                )
            else:
                node.parent_node.from_clause = node.parent_node.from_clause.join(junction_table, onclause=traversal_onclause)
            final_edge = edge.final_edge
            source_col = final_edge.source_col
            sink_col = final_edge.sink_col
            if node.block.direction == 'in':
                source_col, sink_col = sink_col, source_col

            outer_column = self._get_column_from_table(junction_table, source_col)
            inner_column = self._get_column_from_table(node.table, sink_col)
            return [(node.from_clause, outer_column==inner_column)]

    @staticmethod
    def _get_column_from_table(table, column_name):
        if not hasattr(table, 'c'):
            raise AssertionError('No columns found on table object {}'.format(table))
        if not hasattr(table.c, column_name):
            raise AssertionError(
                'No column for table "{}" with name "{}"'.format(table, column_name)
            )
        return getattr(table.c, column_name)

    def get_edge(self, node):
        edge_name = node.block.edge_name
        if not isinstance(node.block, blocks.Recurse):
            outer_type_name = node.outer_type
            relative_type = node.relative_type
        else:
            # this is a recursive edge, from a type back onto itself
            outer_type_name = node.relative_type
            relative_type = node.relative_type
        if outer_type_name in self.config:
            parent_config = self.config[outer_type_name]
            if 'edges' in parent_config:
                edges = parent_config['edges']
                if edge_name in edges:
                    return edges[edge_name]
        outer_table = self._get_table_for_schema_name(outer_type_name)
        inner_table = self._get_table_for_schema_name(relative_type)
        inner_table_fks = [fk for fk in inner_table.foreign_keys if fk.column.table == outer_table]
        outer_table_fks = [fk for fk in outer_table.foreign_keys if fk.column.table == inner_table]
        outer_matches = [foreign_key for foreign_key in inner_table_fks if
                         foreign_key.column.name in outer_table.columns]
        inner_matches = [foreign_key for foreign_key in outer_table_fks if
                         foreign_key.column.name in inner_table.columns]
        if len(outer_matches) == 1 and len(inner_matches) == 0:
            fk = outer_matches[0]
            return BasicEdge(source_column=fk.column.name, sink_column=fk.parent.name)
        elif len(inner_matches) == 1 and len(outer_matches) == 0:
            fk = inner_matches[0]
            return BasicEdge(source_column=fk.parent.name, sink_column=fk.column.name)
        elif len(inner_matches) == 0 and len(outer_matches) == 0:
            raise AssertionError(
                'No foreign key found from type {} to type {}'.format(outer_type_name, relative_type)
            )
        else:
            raise AssertionError('Ambiguous foreign key specified.')



