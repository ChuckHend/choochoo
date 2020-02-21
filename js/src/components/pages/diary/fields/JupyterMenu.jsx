import React from "react";
import {Button, Menu, MenuItem, Grid, Link} from "@material-ui/core";
import {Text, zip} from "../../../utils";
import MenuIcon from '@material-ui/icons/Menu'
import {makeStyles} from "@material-ui/core/styles";


const useStyles = makeStyles(theme => ({
    icon: {
        verticalAlign: 'middle',
    },
}));


export default function JupyterMenu(props) {

    const {json, label, template, params} = props;
    const [, ...rest] = json;
    const [anchor, setAnchor] = React.useState(null);
    const classes = useStyles();

    const handleClick = event => {
        setAnchor(event.currentTarget);
    };

    const handleClose = () => {
        setAnchor(null);
    };

    const items = rest.map(row => {
        const urlArgs = zip(params, row.db).map(([name, value]) => name + '=' + value).join('&');
        return (<MenuItem onClick={handleClose}>
            <Link href={'jupyter/' + template + '?' + urlArgs} target='_blank'>{row.value}</Link>
        </MenuItem>);
    });

    return (<Grid xs={4}>
        <Button onClick={handleClick}>
            <MenuIcon className={classes.icon}/>
            <Text>{label}</Text>
        </Button>
        <Menu anchorEl={anchor} keepMounted open={Boolean(anchor)} onClose={handleClose}>{items}</Menu>
    </Grid>);
}