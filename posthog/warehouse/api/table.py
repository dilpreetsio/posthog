from typing import Any

from rest_framework import exceptions, filters, request, response, serializers, status, viewsets
from posthog.api.utils import action

from posthog.api.routing import TeamAndOrgViewSetMixin
from posthog.api.shared import UserBasicSerializer
from posthog.hogql.context import HogQLContext
from posthog.hogql.database.database import SerializedField, create_hogql_database, serialize_fields
from posthog.schema import DatabaseSerializedFieldType
from posthog.tasks.warehouse import validate_data_warehouse_table_columns
from posthog.warehouse.models import (
    DataWarehouseCredential,
    DataWarehouseTable,
)
from posthog.warehouse.api.external_data_source import SimpleExternalDataSourceSerializers
from posthog.warehouse.models.table import CLICKHOUSE_HOGQL_MAPPING, SERIALIZED_FIELD_TO_CLICKHOUSE_MAPPING


class CredentialSerializer(serializers.ModelSerializer):
    created_by = UserBasicSerializer(read_only=True)

    class Meta:
        model = DataWarehouseCredential
        fields = ["id", "created_by", "created_at", "access_key", "access_secret"]
        read_only_fields = [
            "id",
            "created_by",
            "created_at",
        ]
        extra_kwargs = {"access_key": {"write_only": "True"}, "access_secret": {"write_only": "True"}}


class TableSerializer(serializers.ModelSerializer):
    created_by = UserBasicSerializer(read_only=True)
    credential = CredentialSerializer()
    columns = serializers.SerializerMethodField(read_only=True)
    external_data_source = SimpleExternalDataSourceSerializers(read_only=True)
    external_schema = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = DataWarehouseTable
        fields = [
            "id",
            "deleted",
            "name",
            "format",
            "created_by",
            "created_at",
            "url_pattern",
            "credential",
            "columns",
            "external_data_source",
            "external_schema",
        ]
        read_only_fields = ["id", "created_by", "created_at", "columns", "external_data_source", "external_schema"]

    def get_columns(self, table: DataWarehouseTable) -> list[SerializedField]:
        database = self.context.get("database", None)
        if not database:
            database = create_hogql_database(team_id=self.context["team_id"])

        if database.has_table(table.name):
            fields = database.get_table(table.name).fields
        else:
            fields = table.hogql_definition().fields

        serializes_fields = serialize_fields(
            fields,
            HogQLContext(database=database, team_id=self.context["team_id"]),
            table.name_chain,
            table.columns,
            table_type="external",
        )

        return [
            SerializedField(
                key=field.name,
                name=field.name,
                type=field.type,
                schema_valid=field.schema_valid,
                fields=field.fields,
                table=field.table,
                chain=field.chain,
            )
            for field in serializes_fields
        ]

    def get_external_schema(self, instance: DataWarehouseTable):
        from posthog.warehouse.api.external_data_schema import SimpleExternalDataSchemaSerializer

        return SimpleExternalDataSchemaSerializer(instance.externaldataschema_set.first(), read_only=True).data or None

    def create(self, validated_data):
        team_id = self.context["team_id"]

        table_name_exists = (
            DataWarehouseTable.objects.exclude(deleted=True)
            .filter(team_id=team_id, name=validated_data["name"])
            .exists()
        )
        if table_name_exists:
            raise exceptions.ValidationError("Table name already exists.")

        validated_data["team_id"] = team_id
        validated_data["created_by"] = self.context["request"].user
        if validated_data.get("credential"):
            validated_data["credential"] = DataWarehouseCredential.objects.create(
                team_id=team_id,
                access_key=validated_data["credential"]["access_key"],
                access_secret=validated_data["credential"]["access_secret"],
            )
        table = DataWarehouseTable(**validated_data)
        try:
            table.columns = table.get_columns()
        except Exception as err:
            raise serializers.ValidationError(str(err))
        table.save()

        validate_data_warehouse_table_columns.delay(self.context["team_id"], str(table.id))

        return table


class SimpleTableSerializer(serializers.ModelSerializer):
    columns = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = DataWarehouseTable
        fields = ["id", "name", "columns", "row_count"]
        read_only_fields = ["id", "name", "columns", "row_count"]

    def get_columns(self, table: DataWarehouseTable) -> list[SerializedField]:
        database = self.context.get("database", None)
        team_id = self.context.get("team_id", None)

        if not database:
            database = create_hogql_database(team_id=self.context["team_id"])

        fields = serialize_fields(
            table.hogql_definition().fields,
            HogQLContext(database=database, team_id=team_id),
            table.name_chain,
            table_type="external",
        )
        return [
            SerializedField(
                key=field.name,
                name=field.name,
                type=field.type,
                schema_valid=field.schema_valid,
                fields=field.fields,
                table=field.table,
                chain=field.chain,
            )
            for field in fields
        ]


class TableViewSet(TeamAndOrgViewSetMixin, viewsets.ModelViewSet):
    """
    Create, Read, Update and Delete Warehouse Tables.
    """

    scope_object = "INTERNAL"
    queryset = DataWarehouseTable.objects.all()
    serializer_class = TableSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["name"]
    ordering = "-created_at"

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        context["database"] = create_hogql_database(team_id=self.team_id)
        context["team_id"] = self.team_id
        return context

    def safely_get_queryset(self, queryset):
        return (
            queryset.filter(team_id=self.team_id)
            .exclude(deleted=True)
            .prefetch_related("created_by", "externaldataschema_set")
            .order_by(self.ordering)
        )

    def destroy(self, request: request.Request, *args: Any, **kwargs: Any) -> response.Response:
        instance: DataWarehouseTable = self.get_object()

        if instance.external_data_source is not None:
            return response.Response(
                status=status.HTTP_400_BAD_REQUEST, data={"message": "Can't delete a sourced table"}
            )

        instance.soft_delete()

        return response.Response(status=status.HTTP_204_NO_CONTENT)

    def perform_update(self, serializer):
        instance = serializer.instance
        validated_data = serializer.validated_data

        credential_data = validated_data.pop("credential", None)
        if credential_data:
            credential = instance.credential
            credential.access_key = credential_data.get("access_key", credential.access_key)
            credential.access_secret = credential_data.get("access_secret", credential.access_secret)
            credential.save()

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

    @action(methods=["POST"], detail=True)
    def update_schema(self, request: request.Request, *args: Any, **kwargs: Any) -> response.Response:
        updates = request.data.get("updates", None)
        if updates is None:
            return response.Response(status=status.HTTP_200_OK)

        table: DataWarehouseTable = self.get_object()
        if table.external_data_source is not None:
            return response.Response(
                status=status.HTTP_400_BAD_REQUEST, data={"message": "The table must be a manually linked table"}
            )

        columns = table.columns
        column_keys: list[str] = columns.keys()
        for key in updates.keys():
            if key not in column_keys:
                return response.Response(
                    status=status.HTTP_400_BAD_REQUEST, data={"message": f"Column {key} does not exist on table"}
                )

        for key, value in updates.items():
            try:
                DatabaseSerializedFieldType[value.upper()]
            except:
                return response.Response(
                    status=status.HTTP_400_BAD_REQUEST,
                    data={"message": f"Can not parse type {value} for column {key} - type does not exist"},
                )

            current_value = columns[key]
            # If the column is in the "old" style, convert it to the new
            if isinstance(current_value, str):
                columns[key] = {}

            columns[key]["clickhouse"] = f"Nullable({SERIALIZED_FIELD_TO_CLICKHOUSE_MAPPING[value]})"
            columns[key]["hogql"] = CLICKHOUSE_HOGQL_MAPPING[SERIALIZED_FIELD_TO_CLICKHOUSE_MAPPING[value]].__name__

        table.columns = columns
        table.save()

        # Have to update the `valid` value separately to the `columns` value as the columns are required in the `ast.S3Table` class when querying ClickHouse
        for key in updates.keys():
            columns[key]["valid"] = table.validate_column_type(key)

        table.columns = columns
        table.save()

        return response.Response(status=status.HTTP_200_OK)

    @action(methods=["POST"], detail=True)
    def refresh_schema(self, request: request.Request, *args: Any, **kwargs: Any) -> response.Response:
        table: DataWarehouseTable = self.get_object()

        table.columns = table.get_columns()
        table.save()

        return response.Response(status=status.HTTP_200_OK)
