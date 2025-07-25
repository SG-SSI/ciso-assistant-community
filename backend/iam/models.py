"""IAM model for CISO Assistant
Inspired from Azure IAM model"""

from collections import defaultdict
from typing import Any, List, Self, Tuple, Generator
import uuid
from allauth.account.models import EmailAddress
from django.utils import timezone
from django.db import models
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AnonymousUser, Permission
from django.utils.translation import gettext_lazy as _
from django.urls.base import reverse_lazy
from knox.models import AuthToken
from core.utils import (
    BUILTIN_USERGROUP_CODENAMES,
    BUILTIN_ROLE_CODENAMES,
)
from core.base_models import AbstractBaseModel, NameDescriptionMixin
from core.utils import UserGroupCodename, RoleCodename
from django.utils.http import urlsafe_base64_encode
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.template.loader import render_to_string
from django.core.mail import send_mail, get_connection, EmailMessage
from django.core.validators import validate_email
from ciso_assistant.settings import (
    CISO_ASSISTANT_URL,
    EMAIL_HOST,
    EMAIL_HOST_USER,
    EMAIL_HOST_USER_RESCUE,
    EMAIL_HOST_PASSWORD_RESCUE,
    EMAIL_HOST_RESCUE,
    EMAIL_PORT,
    EMAIL_PORT_RESCUE,
    EMAIL_USE_TLS,
    EMAIL_USE_TLS_RESCUE,
)
from django.conf import settings

import structlog

logger = structlog.get_logger(__name__)

from auditlog.registry import auditlog


def _get_root_folder():
    """helper function outside of class to facilitate serialization
    to be used only in Folder class"""
    try:
        return Folder.objects.get(content_type=Folder.ContentType.ROOT)
    except:
        return None


class Folder(NameDescriptionMixin):
    """A folder is a container for other folders or any object
    Folders are organized in a tree structure, with a single root folder
    Folders are the base perimeter for role assignments
    """

    @staticmethod
    def get_root_folder() -> Self:
        """class function for general use"""
        return _get_root_folder()

    @staticmethod
    def get_root_folder_id() -> uuid.UUID:
        """class function for general use"""
        try:
            return _get_root_folder().id
        except:
            return _get_root_folder()

    class ContentType(models.TextChoices):
        """content type for a folder"""

        ROOT = "GL", _("GLOBAL")
        DOMAIN = "DO", _("DOMAIN")
        ENCLAVE = "EN", _("ENCLAVE")

    content_type = models.CharField(
        max_length=2, choices=ContentType.choices, default=ContentType.DOMAIN
    )

    parent_folder = models.ForeignKey(
        "self",
        null=True,
        on_delete=models.CASCADE,
        verbose_name=_("parent folder"),
        default=_get_root_folder,
    )
    builtin = models.BooleanField(default=False)

    fields_to_check = ["name"]

    class Meta:
        """for Model"""

        verbose_name = _("Folder")
        verbose_name_plural = _("Folders")

    def __str__(self) -> str:
        return self.name.__str__()

    def get_sub_folders(self) -> Generator[Self, None, None]:
        """Return the list of subfolders"""

        def sub_folders_in(folder):
            for sub_folder in folder.folder_set.all():
                yield sub_folder
                yield from sub_folders_in(sub_folder)

        yield from sub_folders_in(self)

    # Should we update data-model.md now that this method is a generator ?
    def get_parent_folders(self) -> Generator[Self, None, None]:
        """Return the list of parent folders"""
        current_folder = self
        while (current_folder := current_folder.parent_folder) is not None:
            yield current_folder

    def get_folder_full_path(self, include_root: bool = False) -> list[Self]:
        """
        Get the full path of the folder including its parents.
        If include_root is True, the root folder is included in the path.
        """
        folders = ([self] + [f for f in self.get_parent_folders()])[::-1]
        if include_root:
            return folders
        return folders[1:] if len(folders) > 1 else folders

    @staticmethod
    def _navigate_structure(start, path):
        """
        Navigate through a mixed structure of objects and dictionaries.

        :param start: The initial object or dictionary from which to start navigating.
        :param path: A list of strings representing the path to navigate, with each element
                     being an attribute name (for objects) or a key (for dictionaries).
        :return: The value found at the end of the path, or None if any part of the path is invalid.
        """
        current = start
        for p in path:
            if isinstance(current, dict):
                # For dictionaries
                current = current.get(p, None)
            else:
                # For objects
                try:
                    current = getattr(current, p, None)
                except AttributeError:
                    # If the attribute doesn't exist and current is not a dictionary
                    return None
            if current is None:
                return None
        return current

    @staticmethod
    def get_folder(obj: Any):
        """
        Return the folder of an object using navigation through mixed structures.
        For a folder, it is the object itself
        """
        if isinstance(obj, Folder):
            return obj
        # Define paths to try in order. Each path is a list representing the traversal path.
        # NOTE: There are probably better ways to represent these, but it works.
        paths = [
            ["folder"],
            ["parent_folder"],
            ["perimeter", "folder"],
            ["entity", "folder"],
            ["provider_entity", "folder"],
            ["solution", "provider_entity", "folder"],
            ["risk_assessment", "perimeter", "folder"],
            ["risk_scenario", "risk_assessment", "perimeter", "folder"],
            ["compliance_assessment", "perimeter", "folder"],
        ]

        # Attempt to traverse each path until a valid folder is found or all paths are exhausted.
        for path in paths:
            folder = Folder._navigate_structure(obj, path)
            if folder is not None:
                return folder

        # If no folder is found after trying all paths, handle this case (e.g., return None or raise an error).
        return None

    @staticmethod
    def create_default_ug_and_ra(folder: Self):
        if folder.content_type == Folder.ContentType.DOMAIN:
            readers = UserGroup.objects.create(
                name=UserGroupCodename.READER, folder=folder, builtin=True
            )
            approvers = UserGroup.objects.create(
                name=UserGroupCodename.APPROVER, folder=folder, builtin=True
            )
            analysts = UserGroup.objects.create(
                name=UserGroupCodename.ANALYST, folder=folder, builtin=True
            )
            managers = UserGroup.objects.create(
                name=UserGroupCodename.DOMAIN_MANAGER, folder=folder, builtin=True
            )
            ra1 = RoleAssignment.objects.create(
                user_group=readers,
                role=Role.objects.get(name=RoleCodename.READER),
                builtin=True,
                folder=Folder.get_root_folder(),
                is_recursive=True,
            )
            ra1.perimeter_folders.add(folder)
            ra2 = RoleAssignment.objects.create(
                user_group=approvers,
                role=Role.objects.get(name=RoleCodename.APPROVER),
                builtin=True,
                folder=Folder.get_root_folder(),
                is_recursive=True,
            )
            ra2.perimeter_folders.add(folder)
            ra3 = RoleAssignment.objects.create(
                user_group=analysts,
                role=Role.objects.get(name=RoleCodename.ANALYST),
                builtin=True,
                folder=Folder.get_root_folder(),
                is_recursive=True,
            )
            ra3.perimeter_folders.add(folder)
            ra4 = RoleAssignment.objects.create(
                user_group=managers,
                role=Role.objects.get(name=RoleCodename.DOMAIN_MANAGER),
                builtin=True,
                folder=Folder.get_root_folder(),
                is_recursive=True,
            )
            ra4.perimeter_folders.add(folder)
            # Clear the cache after a new folder is created - purposely clearing everything


class FolderMixin(models.Model):
    """
    Add foreign key to Folder, defaults to root folder
    """

    folder = models.ForeignKey(
        Folder,
        on_delete=models.CASCADE,
        related_name="%(class)s_folder",
        default=Folder.get_root_folder_id,
    )

    def get_folder_full_path(self, include_root: bool = False) -> list[Folder]:
        folders = ([self.folder] + [f for f in self.folder.get_parent_folders()])[::-1]
        if include_root:
            return folders
        return folders[1:] if len(folders) > 1 else folders

    class Meta:
        abstract = True


class PublishInRootFolderMixin(models.Model):
    """
    Set is_published to True if object is attached to the root folder
    """

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if (
            getattr(self, "folder") == Folder.get_root_folder()
            and hasattr(self, "is_published")
            and not self.is_published
        ):
            self.is_published = True
        super().save(*args, **kwargs)


class UserGroup(NameDescriptionMixin, FolderMixin):
    """UserGroup objects contain users and can be used as principals in role assignments"""

    builtin = models.BooleanField(default=False)

    class Meta:
        """for Model"""

        verbose_name = _("user group")
        verbose_name_plural = _("user groups")

    def __str__(self) -> str:
        if self.builtin:
            return f"{self.folder.name} - {BUILTIN_USERGROUP_CODENAMES.get(self.name)}"
        return self.name

    def get_name_display(self) -> str:
        return self.name

    def get_localization_dict(self) -> dict:
        return {
            "folder": self.folder.name,
            "role": BUILTIN_USERGROUP_CODENAMES.get(self.name),
        }

    @property
    def permissions(self):
        return RoleAssignment.get_permissions(self)


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(
        self,
        email: str,
        password: str,
        mailing: bool,
        initial_group: UserGroup,
        **extra_fields,
    ):
        """
        Create and save a user with the given email, and password.
        If mailing is set, send a welcome mail
        If initial_group is given, put the new user in this group
        On mail error, raise a corresponding exception, but the user is properly created
        TODO: find a better way to manage mailing error
        """
        validate_email(email)
        email = self.normalize_email(email)
        user = self.model(
            email=email,
            first_name=extra_fields.get("first_name", ""),
            last_name=extra_fields.get("last_name", ""),
            is_superuser=extra_fields.get("is_superuser", False),
            is_active=extra_fields.get("is_active", True),
            folder=_get_root_folder(),
            keep_local_login=extra_fields.get("keep_local_login", False),
        )
        user.user_groups.set(extra_fields.get("user_groups", []))
        if password:
            user.password = make_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        if initial_group:
            initial_group.user_set.add(user)

        # create an EmailAddress object for the newly created user
        # this is required by allauth
        EmailAddress.objects.create(
            user=user,
            email=user.email,
            verified=True,
            primary=True,
        )

        logger.info("user created sucessfully", user=user)

        if mailing:
            template_name = (
                "registration/first_connexion_email.html"
                if user.is_local
                else "registration/first_connexion_email_sso.html"
            )
            try:
                user.mailing(
                    email_template_name=template_name,
                    subject=_("Welcome to Ciso Assistant!"),
                )
            except Exception as exception:
                print(f"sending email to {email} failed")
                raise exception
        return user

    def create_user(self, email: str, password: str = None, **extra_fields):
        """create a normal user following Django convention"""
        logger.info("creating user", email=email)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(
            email=email,
            password=password,
            mailing=(EMAIL_HOST or EMAIL_HOST_RESCUE),
            initial_group=None,
            **extra_fields,
        )

    def create_superuser(self, email: str, password: str = None, **extra_fields):
        """create a superuser following Django convention"""
        logger.info("creating superuser", email=email)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        superuser = self._create_user(
            email=email,
            password=password,
            mailing=not (password) and (EMAIL_HOST or EMAIL_HOST_RESCUE),
            initial_group=UserGroup.objects.get(name="BI-UG-ADM"),
            keep_local_login=True,
            **extra_fields,
        )
        return superuser


class CaseInsensitiveUserManager(UserManager):
    def get_by_natural_key(self, username):
        """
        By default, Django does a case-sensitive check on usernames™.
        Overriding this method fixes it.
        """
        return self.get(**{self.model.USERNAME_FIELD + "__iexact": username})


class User(AbstractBaseUser, AbstractBaseModel, FolderMixin):
    """a user is a principal corresponding to a human"""

    last_name = models.CharField(_("last name"), max_length=150, blank=True)
    first_name = models.CharField(_("first name"), max_length=150, blank=True)
    email = models.CharField(max_length=100, unique=True)
    first_login = models.BooleanField(default=True)
    preferences = models.JSONField(default=dict)
    keep_local_login = models.BooleanField(
        default=False,
        help_text=_(
            "If True allow the user to log in using the normal login form even with SSO forced."
        ),
    )
    is_third_party = models.BooleanField(default=False)
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_(
            "Designates whether this user should be treated as active. "
            "Unselect this instead of deleting accounts."
        ),
    )
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now)
    is_superuser = models.BooleanField(
        _("superuser status"),
        default=False,
        help_text=_(
            "Designates that this user has all permissions without explicitly assigning them."
        ),
    )
    user_groups = models.ManyToManyField(
        UserGroup,
        verbose_name=_("user groups"),
        blank=True,
        help_text=_(
            "The user groups this user belongs to. A user will get all permissions "
            "granted to each of their user groups."
        ),
    )
    objects = CaseInsensitiveUserManager()

    # USERNAME_FIELD is used as the unique identifier for the user
    # and is required by Django to be set to a non-empty value.
    # See https://docs.djangoproject.com/en/3.2/topics/auth/customizing/#django.contrib.auth.models.CustomUser.USERNAME_FIELD
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        """for Model"""

        verbose_name = _("user")
        verbose_name_plural = _("users")
        #        swappable = 'AUTH_USER_MODEL'
        permissions = (("backup", "backup"), ("restore", "restore"))

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        logger.info("user deleted", user=self)

    def save(self, *args, **kwargs):
        if self.is_superuser and not self.is_active:
            # avoid deactivation of superuser
            self.is_active = True
        if not self.is_local:
            self.set_unusable_password()
        super().save(*args, **kwargs)
        logger.info("user saved", user=self)

    def __str__(self):
        return (
            f"{self.first_name} {self.last_name}"
            if self.first_name and self.last_name
            else self.email
        )

    def get_full_name(self) -> str:
        """get user's full name (i.e. first_name + last_name)"""
        try:
            full_name = f"{self.first_name} {self.last_name}"
            return full_name
        except:
            return ""

    def get_short_name(self) -> str:
        """get user's short name (i.e. first_name or email before @))"""
        return self.first_name if self.first_name else self.email.split("@")[0]

    def mailing(self, email_template_name, subject, object="", object_id="", pk=False):
        """
        Sending a mail to a user for password resetting or creation
        """
        header = {
            "email": self.email,
            "root_url": CISO_ASSISTANT_URL,
            "uid": urlsafe_base64_encode(force_bytes(self.pk)),
            "user": self,
            "token": default_token_generator.make_token(self),
            "protocol": "https",
            "pk": str(pk) if pk else None,
            "object": object,
            "object_id": object_id,
        }
        email = render_to_string(email_template_name, header)
        try:
            send_mail(
                subject,
                email,
                None,
                [self.email],
                fail_silently=False,
                html_message=email,
            )
            logger.info("email sent", recipient=self.email, subject=subject)
        except Exception as primary_exception:
            logger.error(
                "primary mailer failure, trying rescue",
                recipient=self.email,
                subject=subject,
                error=primary_exception,
                email_host=EMAIL_HOST,
                email_port=EMAIL_PORT,
                email_host_user=EMAIL_HOST_USER,
                email_use_tls=EMAIL_USE_TLS,
            )
            if EMAIL_HOST_RESCUE:
                try:
                    with get_connection(
                        host=EMAIL_HOST_RESCUE,
                        port=EMAIL_PORT_RESCUE,
                        username=EMAIL_HOST_USER_RESCUE,
                        password=EMAIL_HOST_PASSWORD_RESCUE,
                        use_tls=EMAIL_USE_TLS_RESCUE if EMAIL_USE_TLS_RESCUE else False,
                    ) as new_connection:
                        EmailMessage(
                            subject,
                            email,
                            None,
                            [self.email],
                            connection=new_connection,
                        ).send()
                    logger.info("email sent", recipient=self.email, subject=subject)
                except Exception as rescue_exception:
                    logger.error(
                        "rescue mailer failure",
                        recipient=self.email,
                        subject=subject,
                        error=rescue_exception,
                        email_host=EMAIL_HOST_RESCUE,
                        email_port=EMAIL_PORT_RESCUE,
                        email_username=EMAIL_HOST_USER_RESCUE,
                        email_use_tls=EMAIL_USE_TLS_RESCUE,
                    )
                    raise rescue_exception
            else:
                raise primary_exception

    def get_user_groups(self):
        """get the list of user groups containing the user in the form (group_name, builtin)"""
        return [(x.__str__(), x.builtin) for x in self.user_groups.all()]

    def get_roles(self):
        """get the list of roles attached to the user"""
        return list(
            self.user_groups.all()
            .values_list("roleassignment__role__name", flat=True)
            .distinct()
        )

    @property
    def has_backup_permission(self) -> bool:
        return RoleAssignment.is_access_allowed(
            user=self,
            perm=Permission.objects.get(codename="backup"),
            folder=Folder.get_root_folder(),
        )

    @property
    def edit_url(self) -> str:
        """get the edit url of the user"""
        return reverse_lazy(f"{self.__class__.__name__.lower()}-update", args=[self.id])

    @property
    def username(self):
        return self.email

    @property
    def permissions(self):
        return RoleAssignment.get_permissions(self)

    @username.setter
    def set_username(self, username):
        self.email = username

    @staticmethod
    def get_admin_users() -> List[Self]:
        return User.objects.filter(user_groups__name="BI-UG-ADM")

    def is_admin(self) -> bool:
        return self.user_groups.filter(name="BI-UG-ADM").exists()

    @property
    def is_editor(self) -> bool:
        permissions = RoleAssignment.get_permissions(self)
        editor_prefixes = {"add_", "change_", "delete_"}
        return any(
            any(perm.startswith(prefix) for prefix in editor_prefixes)
            for perm in permissions
        )

    @property
    def is_local(self) -> bool:
        """
        Indicates whether the user can log in using a local password
        """
        from global_settings.models import GlobalSettings

        try:
            sso_settings = GlobalSettings.objects.get(
                name=GlobalSettings.Names.SSO
            ).value
        except GlobalSettings.DoesNotExist:
            sso_settings = {}

        return self.is_active and (
            self.keep_local_login
            or not sso_settings.get("is_enabled", False)
            or not sso_settings.get("force_sso", False)
        )

    @classmethod
    def get_editors(cls) -> List[Self]:
        return [
            user
            for user in cls.objects.all()
            if user.is_editor and not user.is_third_party
        ]


class Role(NameDescriptionMixin, FolderMixin):
    """A role is a list of permissions"""

    permissions = models.ManyToManyField(
        Permission,
        verbose_name=_("permissions"),
        blank=True,
    )
    builtin = models.BooleanField(default=False)

    def __str__(self) -> str:
        if self.builtin:
            return f"{BUILTIN_ROLE_CODENAMES.get(self.name)}"
        return self.name


class RoleAssignment(NameDescriptionMixin, FolderMixin):
    """fundamental class for CISO Assistant RBAC model, similar to Azure IAM model"""

    perimeter_folders = models.ManyToManyField(
        "Folder", verbose_name=_("Domain"), related_name="perimeter_folders"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE
    )
    user_group = models.ForeignKey(UserGroup, null=True, on_delete=models.CASCADE)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, verbose_name=_("Role"))
    is_recursive = models.BooleanField(_("sub folders are visible"), default=False)
    builtin = models.BooleanField(default=False)

    def __str__(self) -> str:
        # pragma pylint: disable=no-member
        return (
            "id="
            + str(self.id)
            + ", folders: "
            + str(list(self.perimeter_folders.values_list("name", flat=True)))
            + ", role: "
            + str(self.role.name)
            + ", user: "
            + (str(self.user.email) if self.user else "/")
            + ", user group: "
            + (str(self.user_group.name) if self.user_group else "/")
        )

    @staticmethod
    def is_access_allowed(
        user: AbstractBaseUser | AnonymousUser, perm: Permission, folder: Folder
    ) -> bool:
        """
        Determines if a user has specified permission on a specified folder
        """
        add_tag_permission = Permission.objects.get(codename="add_filteringlabel")
        for ra in RoleAssignment.get_role_assignments(user):
            if (
                (perm == add_tag_permission) and perm in ra.role.permissions.all()
            ):  # Allow any user to add tags if he has the permission
                return True
            f = folder
            while f is not None:
                if (
                    f in ra.perimeter_folders.all()
                    and perm in ra.role.permissions.all()
                ):
                    return True
                f = f.parent_folder
        return False

    @staticmethod
    def is_object_readable(
        user: AbstractBaseUser | AnonymousUser, object_type: Any, id: uuid
    ) -> bool:
        """
        Determines if a user has read on an object by id
        """
        obj = object_type.objects.filter(id=id).first()
        if not obj:
            return False
        class_name = object_type.__name__.lower()
        permission = Permission.objects.get(codename="view_" + class_name)
        return RoleAssignment.is_access_allowed(
            user, permission, Folder.get_folder(obj)
        )

    @staticmethod
    def get_accessible_folders(
        folder: Folder,
        user: User,
        content_type: Folder.ContentType,
        codename: str = "view_folder",
    ) -> list[Folder]:
        """Gets the list of folders with specified contentType that can be viewed by a user from a given folder
        If the contentType is not specified, returns all accessible folders
        Returns the list of the ids of the matching folders
        If permission is specified, returns accessible folders which can be altered with this specific permission
        """
        folders_set = set()
        ref_permission = Permission.objects.get(codename=codename)
        # first get all accessible folders, independently of contentType
        for ra in [
            x
            for x in RoleAssignment.get_role_assignments(user)
            if (
                (
                    Permission.objects.get(codename="view_folder")
                    in x.role.permissions.all()
                )
                and (ref_permission in x.role.permissions.all())
            )
        ]:
            for f in ra.perimeter_folders.all():
                folders_set.add(f)
                folders_set.update(f.get_sub_folders())
        # calculate perimeter
        perimeter = set()
        perimeter.add(folder)
        perimeter.update(folder.get_sub_folders())
        # return filtered result
        return [
            x.id
            for x in folders_set
            if (x.content_type == content_type if content_type else True)
            and x in perimeter
        ]

    @staticmethod
    def get_accessible_object_ids(
        folder: Folder, user: AbstractBaseUser | AnonymousUser, object_type: Any
    ) -> Tuple["list[Any]", "list[Any]", "list[Any]"]:
        """Gets all objects of a specified type that a user can reach in a given folder
        Only accessible folders are considered
        Returns a triplet: (view_objects_list, change_object_list, delete_object_list)
        Assumes that object type follows Django conventions for permissions
        Also retrieve published objects in view
        """
        class_name = object_type.__name__.lower()
        permission_view = Permission.objects.get(codename="view_" + class_name)
        permission_change = Permission.objects.get(codename="change_" + class_name)
        permission_delete = Permission.objects.get(codename="delete_" + class_name)
        permissions = set([permission_view, permission_change, permission_delete])
        result_view = set()
        result_change = set()
        result_delete = set()

        ref_permission = Permission.objects.get(codename="view_folder")
        perimeter = {folder} | set(folder.get_sub_folders())
        # Process role assignments
        role_assignments = [
            ra
            for ra in RoleAssignment.get_role_assignments(user)
            if ref_permission in ra.role.permissions.all()
        ]
        result_folders = defaultdict(set)
        for ra in role_assignments:
            ra_permissions = set(ra.role.permissions.all())
            ra_perimeter = set(ra.perimeter_folders.all())
            if ra.is_recursive:
                ra_perimeter.update(
                    *[folder.get_sub_folders() for folder in ra_perimeter]
                )
            target_folders = perimeter & ra_perimeter
            for p in permissions & ra_permissions:
                for f in target_folders:
                    result_folders[f].add(p)
        for f in result_folders:
            if hasattr(object_type, "folder"):
                objects_ids = object_type.objects.filter(folder=f).values_list(
                    "id", flat=True
                )
            elif hasattr(object_type, "risk_assessment"):
                objects_ids = object_type.objects.filter(
                    risk_assessment__folder=f
                ).values_list("id", flat=True)
            elif hasattr(object_type, "entity"):
                objects_ids = object_type.objects.filter(entity__folder=f).values_list(
                    "id", flat=True
                )
            elif hasattr(object_type, "provider_entity"):
                objects_ids = object_type.objects.filter(
                    provider_entity__folder=f
                ).values_list("id", flat=True)
            elif hasattr(object_type, "parent_folder"):
                objects_ids = [f.id]
            else:
                raise NotImplementedError("type not supported")
            if permission_view in result_folders[f]:
                result_view.update(objects_ids)
            if permission_change in result_folders[f]:
                result_change.update(objects_ids)
            if permission_delete in result_folders[f]:
                result_delete.update(objects_ids)

        if hasattr(object_type, "is_published") and hasattr(object_type, "folder"):
            # we assume only objects with a folder attribute are worth publishing
            folders_with_local_view = [
                f for f in result_folders if permission_view in result_folders[f]
            ]
            for my_folder in folders_with_local_view:
                if my_folder.content_type != Folder.ContentType.ENCLAVE:
                    my_folder2 = my_folder.parent_folder
                    while my_folder2:
                        result_view.update(
                            object_type.objects.filter(
                                folder=my_folder2, is_published=True
                            ).values_list("id", flat=True)
                        )
                        my_folder2 = my_folder2.parent_folder

        return (list(result_view), list(result_change), list(result_delete))

    def is_user_assigned(self, user) -> bool:
        """Determines if a user is assigned to the role assignment"""
        return user == self.user or (
            self.user_group and self.user_group in user.user_groups.all()
        )

    @staticmethod
    def get_role_assignments(principal: AbstractBaseUser | AnonymousUser | UserGroup):
        """get all role assignments attached to a user directly or indirectly"""
        assignments = list(principal.roleassignment_set.all())
        if hasattr(principal, "user_groups"):
            for user_group in principal.user_groups.all():
                assignments += list(user_group.roleassignment_set.all())
        assignments += list(principal.roleassignment_set.all())
        return assignments

    @staticmethod
    def get_permissions(principal: AbstractBaseUser | AnonymousUser | UserGroup):
        """get all permissions attached to a user directly or indirectly"""
        permissions = {}
        for ra in RoleAssignment.get_role_assignments(principal):
            for p in ra.role.permissions.all():
                permission_dict = {p.codename: {"str": str(p)}}
                permissions.update(permission_dict)

        return permissions

    @staticmethod
    def has_role(user: AbstractBaseUser | AnonymousUser, role: Role):
        """
        Determines if a user has a specific role.
        """
        for ra in RoleAssignment.get_role_assignments(user):
            if ra.role == role:
                return True
        return False

    @classmethod
    def get_permissions_per_folder(
        cls, principal: AbstractBaseUser | AnonymousUser | UserGroup, recursive=False
    ):
        """
        Get all permissions attached to a user directly or indirectly, grouped by folder.
        If recursive is set to True, permissions from recursive role assignments are transmitted
        to the children of its perimeter folders.
        """
        permissions = defaultdict(set)
        for ra in cls.get_role_assignments(principal):
            ra_permissions = set(
                ra.role.permissions.all().values_list("codename", flat=True)
            )
            for folder in ra.perimeter_folders.all():
                permissions[str(folder.id)] |= ra_permissions
                if recursive and ra.is_recursive:
                    for f in folder.get_sub_folders():
                        permissions[str(f.id)] |= ra_permissions
        return permissions


class PersonalAccessToken(models.Model):
    """
    Personal Access Token model.
    """

    name = models.CharField(max_length=255)
    auth_token = models.ForeignKey(AuthToken, on_delete=models.CASCADE)

    @property
    def created(self):
        return self.auth_token.created

    @property
    def expiry(self):
        return self.auth_token.expiry

    @property
    def digest(self):
        return self.auth_token.digest

    def __str__(self):
        return f"{self.auth_token.user.email} : {self.name} : {self.auth_token.digest}"


auditlog.register(
    User,
    m2m_fields={"user_groups"},
    exclude_fields=["created_at", "updated_at", "password"],
)
