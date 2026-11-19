from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_leadership'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='sitesettings',
            options={'permissions': [('manage_registration_status', 'Can open or close membership registration')], 'verbose_name': 'Site Settings', 'verbose_name_plural': 'Site Settings'},
        ),
        migrations.AddField(
            model_name='sitesettings',
            name='registration_open',
            field=models.BooleanField(default=True, help_text="Tick to accept new public membership applications. Untick to close registration: visitors then see a 'registration is closed' message instead of the form. Existing members, logins, elections and admin work are not affected.", verbose_name='Membership registration is open'),
        ),
    ]
