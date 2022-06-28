import ipaddress

from django.conf import settings
from django.db.models import Q
from django.http import Http404
from django_eventstream import send_event
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import ActionType, Entry, History
from .exceptions import PrefixTooLarge
from .serializers import ActionTypeSerializer, EntrySerializer


class ActionTypeViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ActionType.objects.all()
    permission_classes = (IsAuthenticated,)
    serializer_class = ActionTypeSerializer
    lookup_field = "name"


class EntryViewSet(viewsets.ModelViewSet):
    queryset = Entry.objects.filter(is_active=True)
    permission_classes = (IsAuthenticated,)
    serializer_class = EntrySerializer
    lookup_value_regex = ".*"
    http_method_names = ["get", "post", "head", "delete"]

    def perform_create(self, serializer):
        actiontype = serializer.validated_data["actiontype"]
        route = serializer.validated_data["route"]

        min_prefix = getattr(settings, f"V{route.version}_MINPREFIX", 0)
        if route.prefixlen < min_prefix:
            raise PrefixTooLarge()

        # Must match a channel name defined in asgi.py
        send_event(actiontype, 'add', {'route': str(route)})

        serializer.save()

        # create history object with the associated entry including username
        entry = Entry.objects.get(route__route=route, actiontype__name=actiontype)
        history = History(entry=entry, who=self.request.user, why="API perform create")
        history.save()
        entry.is_active = True
        entry.save()

    @staticmethod
    def find_entries(arg, active_filter=None):
        if not arg:
            return Entry.objects.none()

        # Is our argument an integer?
        try:
            pk = int(arg)
            query = Q(pk=pk)
        except ValueError:
            # Maybe a CIDR? We want the ValueError at this point, if not.
            cidr = ipaddress.ip_network(arg, strict=False)

            min_prefix = getattr(settings, f"V{cidr.version}_MINPREFIX", 0)
            if cidr.prefixlen < min_prefix:
                raise PrefixTooLarge()

            query = Q(route__route__net_overlaps=cidr)

        if active_filter is not None:
            query &= Q(is_active=active_filter)

        return Entry.objects.filter(query)

    def retrieve(self, request, pk=None, **kwargs):
        entries = self.find_entries(pk, active_filter=True)
        # TODO: What happens if we get multiple? Is that ok? I think yes, and return them all?
        if entries.count() != 1:
            raise Http404
        serializer = EntrySerializer(entries, many=True, context={"request": request})
        return Response(serializer.data)

    def destroy(self, request, pk=None, *args, **kwargs):
        entries = self.find_entries(pk, active_filter=True)
        # TODO: What happens if we get multiple? Is that ok? I think no, and don't delete them all?
        if entries.count() == 1:
            # create history object with the associated entry including username
            entry = entries[0]
            actiontype = str(entry.actiontype)
            route = entry.route
            history = History(entry=entry, who=request.user, why="API destroy function")
            history.save()
            entry.is_active = False
            entry.save()

            send_event(actiontype, 'remove', {'route': str(route)})

        return Response(status=status.HTTP_204_NO_CONTENT)
