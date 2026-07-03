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
]
