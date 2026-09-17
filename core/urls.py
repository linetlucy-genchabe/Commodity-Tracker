from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # The site root always lands on CHP Stock Flow now (the old facility-based
    # MOH 748 dashboard below is empty in production -- nothing has ever been
    # uploaded to it there). The legacy view/URL name ("home") is kept working
    # at /legacy/ because base.html's user-menu dropdown still links to it on
    # purpose.
    path("", RedirectView.as_view(pattern_name="chp_home", permanent=False), name="root"),
    path("legacy/", views.home, name="home"),
    path("facility/<int:facility_id>/", views.facility_drilldown, name="facility_drilldown"),
    path("upload/", views.upload_moh748, name="upload"),
    path("upload/<int:upload_id>/delete/", views.delete_moh748_upload, name="delete_upload"),
    path("chp/", views.chp_commodity_home, name="chp_home"),
    path("chp/moh748/", views.chp_moh748_page, name="chp_moh748"),
    path("chp/s11/", views.chp_s11_page, name="chp_s11"),
    path("chp/upload/", views.upload_chp_commodity, name="chp_upload"),
    path("chp/upload/<int:upload_id>/delete/", views.delete_chp_commodity_upload, name="delete_chp_upload"),
    path("chp/export/balances.csv", views.export_chp_balances_csv, name="chp_export_balances"),
    path("chp/export/moh748.csv", views.export_chp_moh748_csv, name="chp_export_moh748"),
    path("chp/export/s11.csv", views.export_chp_s11_csv, name="chp_export_s11"),
    path("chp/export/stock-status.csv", views.export_chp_stock_status_csv, name="chp_export_stock_status"),
]
