from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    # PART 10: dashboards
    path("", views.overview_dashboard_view, name="overview"),
    path("membership/", views.membership_dashboard_view, name="membership_dashboard"),
    path("courses/", views.course_dashboard_view, name="course_dashboard"),
    path("institutions/", views.institution_dashboard_view, name="institution_dashboard"),
    path("age/", views.age_dashboard_view, name="age_dashboard"),
    path("elections/", views.election_dashboard_list_view, name="election_dashboard_list"),
    path("elections/<int:pk>/", views.election_dashboard_detail_view, name="election_dashboard_detail"),
    # PART 11: JSON API
    path("api/membership/", views.api_membership_statistics, name="api_membership"),
    path("api/courses/", views.api_course_statistics, name="api_courses"),
    path("api/institutions/", views.api_institution_statistics, name="api_institutions"),
    path("api/age-distribution/", views.api_age_distribution, name="api_age_distribution"),
    path("api/registration-growth/", views.api_registration_growth, name="api_registration_growth"),
    path("api/elections/<int:pk>/results/", views.api_election_results, name="api_election_results"),
    path("api/elections/<int:pk>/turnout/", views.api_election_turnout, name="api_election_turnout"),
]
