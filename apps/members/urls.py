from django.urls import path

from . import views

app_name = "members"

urlpatterns = [
    path("register/", views.register_view, name="register"),
    path(
        "register/success/<str:application_number>/",
        views.registration_success_view,
        name="registration_success",
    ),
    path("status/", views.status_check_view, name="status_check"),
    path("portal/login/", views.portal_login_view, name="portal_login"),
    path("portal/", views.portal_dashboard_view, name="portal_dashboard"),
    path("portal/profile/", views.portal_profile_view, name="portal_profile"),
    path("portal/logout/", views.portal_logout_view, name="portal_logout"),
]
