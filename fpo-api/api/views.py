"""
    REST API Documentation for Family Protection Order

    OpenAPI spec version: v1


    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
"""

from django.utils import timezone
from datetime import datetime
import json

from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.middleware.csrf import get_token
from django.template.loader import get_template

from rest_framework.views import APIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework import filters as default_filters, generics, permissions

from django_filters import rest_framework as filters

from django_filters.rest_framework import DjangoFilterBackend

from django.db.models import Count, Case, IntegerField, When, F, Q

from api.auth import (
    get_login_uri,
    get_logout_uri,
    grecaptcha_verify,
    grecaptcha_site_key,
)
from api.models import TicketResponse, User, Location, Region, PreparedPdf
from api.pdf import render as render_pdf
from api.send_email import send_email
from api.utils import generate_pdf
from api.serializers import TicketResponseSerializer, LocationSerializer, RegionSerializer, LocationLookupSerializer, RegionLookupSerializer
from django.http import FileResponse

import io
from django.core.files.base import File

class AcceptTermsView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request: Request):
        request.user.accepted_terms_at = datetime.now()
        request.user.save()
        return Response({"ok": True})


class UserStatusView(APIView):
    def get(self, request: Request):
        logged_in = isinstance(request.user, User)
        info = {
            "accepted_terms_at": logged_in and request.user.accepted_terms_at or None,
            "user_id": logged_in and request.user.authorization_id or None,
            "email": logged_in and request.user.email or None,
            "first_name": logged_in and request.user.first_name or None,
            "last_name": logged_in and request.user.last_name or None,
            "login_uri": get_login_uri(request),
            "logout_uri": get_logout_uri(request),
            "surveys": [],
        }
        if logged_in and request.auth == "demo":
            info["demo_user"] = True
        ret = Response(info)
        uid = request.META.get("HTTP_X_DEMO_LOGIN")
        if uid and logged_in:
            # remember demo user
            ret.set_cookie("x-demo-login", uid)
        elif request.COOKIES.get("x-demo-login") and not logged_in:
            # logout
            ret.delete_cookie("x-demo-login")
        ret.set_cookie("csrftoken", get_token(request))
        return ret


class SurveyPdfView(generics.GenericAPIView):
    # FIXME - restore authentication?
    permission_classes = ()  # permissions.IsAuthenticated,)

    def post(self, request: Request, name=None):
        tpl_name = "survey-{}.html".format(name)
        # return HttpResponseBadRequest('Unknown survey name')

        responses = json.loads(request.POST["data"])
        # responses = {'question1': 'test value'}

        template = get_template(tpl_name)
        html_content = template.render(responses)

        if name == "primary":
            instruct_template = get_template("instructions-primary.html")
            instruct_html = instruct_template.render(responses)
            docs = (instruct_html,) + (html_content,) * 4
            pdf_content = render_pdf(*docs)

        else:
            pdf_content = render_pdf(html_content)

        response = HttpResponse(content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="report.pdf"'

        response.write(pdf_content)

        return response


class SubmitTicketResponseView(APIView):
    def get(self, request: Request, name=None):
        key = grecaptcha_site_key()
        return Response({"key": key})

    def post(self, request: Request, name=None):
        check_captcha = grecaptcha_verify(request)
        if not check_captcha["status"]:
            return HttpResponseForbidden(text=check_captcha["message"])

        result = request.data
        disputant = result.get("disputantName", {})
        # address = result.get("disputantAddress", {})
        ticketNumber = result.get("ticketNumber", {})
        ticketNumber = str(ticketNumber.get("prefix")) + str(ticketNumber.get("suffix"))

        response = TicketResponse(
            first_name=disputant.get("first"),
            middle_name=disputant.get("middle"),
            last_name=disputant.get("last"),
            email=result.get("disputantEmail"),
            ticket_number=ticketNumber.upper(),
            ticket_date=result.get("ticketDate"),
            hearing_location_id=result.get("hearingLocation"),
            hearing_attendance=result.get("hearingAttendance"),
            dispute_type=result.get("disputeType")
        )

        check_required = [
            "first_name",
            "last_name",
            "email",
            "ticket_number",
            "ticket_date",
            "hearing_location_id",
            "hearing_attendance",
            "dispute_type",
        ]

        for fname in check_required:
            if not getattr(response, fname):
                return HttpResponseBadRequest()
        # FIXME add required fields here
        # check terms acceptance
        # if not result.get("disputantAcknowledgement"):
        #     return HttpResponseBadRequest()

        #Save the result to DB
        response.save()

        #Generate and Save the pdf to DB
        pdf_content = generate_pdf(result)
        pdf_response = PreparedPdf(
            data = pdf_content
        )
        pdf_response.save()
        response.prepared_pdf_id = pdf_response.pk; 
        response.printed_date = timezone.now()
        
        response.save()

        #Generate and Send the email with pdf attached
        email = result.get("disputantEmail")
        pdf = pdf_response.data
        
        try:
            send_email(email, pdf)
            response.emailed_date = timezone.now()
            response.save()
        except Exception as ex:
            print("Error",ex)
            return Response({"id": response.pk,"PdfId":pdf_response.pk,"Email-sent":False})
        
    
      # {
        #     "disputantName": {"first": "first", "middle": "middle", "last": "last"},
        #     "disputantAddress": {
        #         "street": "addr",
        #         "city": "",
        #         "state": "BC",
        #         "country": "CAN",
        #         "postcode": "",
        #     },
        #     "disputantPhoneNumber": "phone",
        #     "disputantPhoneType": ["item2"],
        #     "disputantEmail": "email",
        #     "ticketNumber": "ticket",
        #     "ticketDate": "2018-04-04",
        #     "hearingLocation": "item2",
        #     "disputeType": "allegation",
        #     "hearingAttendance": "remotely",
        #     "hearingAttendancePhone": "n",
        #     "hearingAttendanceVideo": "y",
        #     "french": "n",
        #     "interpreter": "n",
        #     "witnesses": "n",
        #     "disputantAcknowledgement": ["item1"],
        # }

        return Response({"id": response.pk,"PdfId":pdf_response.pk, "Email-sent":True})


class TicketResponseListFilter(filters.FilterSet):
    is_printed = filters.BooleanFilter(field_name='printed_by', lookup_expr='isnull', exclude=True)
    region = filters.NumberFilter(field_name='hearing_location__region_id', lookup_expr='exact')
    class Meta:
     
        fields = [
            'region',
            'hearing_attendance',
            'dispute_type',
            'is_printed',
            'ticket_number',
            'hearing_location__name',
            'printed_by__name'
        ]

class TicketCountView(APIView):
    def get(self, request: Request):
        return Response({
            'new_count': {
                'by_region': Region.objects.values('name', 'id').annotate(count=Count('region_location__location_ticket__id', filter=Q(region_location__location_ticket__printed_by__isnull=True))),
                'total': TicketResponse.objects.filter(printed_by__isnull=True).aggregate(count=Count('hearing_location__region'))
            },
            'archive_count': {
                'by_region': Region.objects.values('name', 'id').annotate(count=Count('region_location__location_ticket__id', filter=Q(region_location__location_ticket__printed_by__isnull=False))),
                'total': TicketResponse.objects.filter(printed_by__isnull=False).aggregate(count=Count('hearing_location__region'))
            }
        })

class TicketResponseListView(generics.ListAPIView):
    queryset = TicketResponse.objects.all()
    serializer_class = TicketResponseSerializer
    filter_backends = [
        DjangoFilterBackend,
        default_filters.SearchFilter,
        default_filters.OrderingFilter,
    ]
    filterset_class = TicketResponseListFilter
    
    search_fields = ["first_name", "middle_name", "last_name", "ticket_number"]
    ordering_fields = [
        "created_date",
        "archived_date",
        "printed_date",
        "ticket_date",
        "deadline_date",
        "hearing_location__name",
        "ticket_number",
        "dispute_type",
        "last_name",
        "first_name",
    ]
    ordering = ["hearing_location__name", "created_date", "last_name"]

class PdfFileView(APIView):
    def get(self, request: Request, id=None):
        if id is None:
            return HttpResponseBadRequest()
        pdf_queryset = PreparedPdf.objects.get(id=id)
        ticket_queryset = TicketResponse.objects.get(prepared_pdf_id=id)
        filename = ticket_queryset.pdf_filename
        if ticket_queryset.pdf_filename is None:
            filename = "needFileName.pdf"
        return FileResponse(io.BytesIO(pdf_queryset.data), as_attachment=False, filename=filename)

    def post(self, request: Request):
        queryset = PreparedPdf.objects.filter(id__in=request.data.get("id"))
        return FileResponse(io.BytesIO(queryset[0].data), as_attachment=False, filename='hello.pdf')


class LocationListView(generics.ListAPIView):
    queryset = ''
    def get(self, request: Request):
        queryset = Location.objects.all()
        serializer = LocationLookupSerializer(queryset, many=True)
        return Response(serializer.data)

class RegionListView(generics.ListAPIView):
    queryset = ''
    def get(self, request: Request):
        queryset = Region.objects.all()
        serializer = RegionLookupSerializer(queryset, many=True)
        return Response(serializer.data)